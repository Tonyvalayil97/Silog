#!/usr/bin/env python3
# app.py – Streamlit UI for Invoice Watcher logic
#
# Upload one or more invoice PDFs → parse → preview → download Excel.

import io
import os
import re
import traceback
from datetime import datetime
from typing import Dict, Any, Optional, List

import pdfplumber
import pandas as pd
import streamlit as st
from openpyxl import Workbook

# ─────────────────────────────────────────────────────────────
# UNIVERSAL INVOICE NUMBER EXTRACTOR
# ─────────────────────────────────────────────────────────────
def extract_invoice_id(filename: str):
    filename = filename.upper()

    # 1. Try SY pattern first
    m = re.search(r"(SY\d+[A-Z]?)", filename)
    if m:
        return m.group(1)

    # 2. Extract first number + optional last letter (ex: 42308, 55671A)
    m = re.search(r"(\d+[A-Z]?)", filename)
    if m:
        return m.group(1)

    # 3. Fallback: return filename without extension
    return os.path.splitext(filename)[0]


# ─────────────────────────────────────────────────────────────
# Fixed header row
# ─────────────────────────────────────────────────────────────
HEADERS: List[str] = [
    "Timestamp", "Filename", "Invoice_Date", "Currency", "Shipper",
    "Weight_KG", "Volume_M3", "Chargeable_KG", "Chargeable_CBM",
    "Pieces", "Subtotal", "Freight_Mode", "Freight_Rate",
]

# ─────────────────────────────────────────────────────────────
# Helper functions + regex (copied from watcher logic)
# ─────────────────────────────────────────────────────────────

_f = lambda s: float(s.replace(",", "")) if s else None
_to_kg = lambda v, u: v if u.lower().startswith("kg") else v * 0.453592

SHIPPER_PAT = re.compile(
    r"SHIPPER\s*:?\s*(.+?)(?:\n{2,}|\n[A-Z][A-Z &/]{3,30}:)", re.I | re.S
)
INVOICE_DATE_PAT = re.compile(
    r"\b(?:INVOICE\s*)?DATE\s+(\d{1,2}\s*[A-Za-z]{3}\s*\d{2,4})", re.I
)
ROW_PIECES_GW_VOL_PAT = re.compile(
    r"Pieces\s*[:\-]?\s*(\d+)\s+G\.?\s*W\.?\s*K?\.?\s*[:\-]?\s*([\d,.]+)\s*KGS?"
    r"\s+Volume\s*[:\-]?\s*([\d,.]+)", re.I
)
ROW_GW_VOL_PAT = re.compile(
    r"G\.?\s*W\.?\s*K?\.?\s*[:\-]?\s*([\d,.]+)\s*KGS?"
    r"\s+Volume\s*[:\-]?\s*([\d,.]+)", re.I
)
PIECES_PAT = re.compile(r"Pieces\s*[:\-]?\s*(\d+)", re.I)
GW_PAT = re.compile(r"G\.?\s*W\.?\s*K?\.?\s*[:\-]?\s*([\d,.]+)\s*KGS?", re.I)
VOL_PAT = re.compile(r"Volume\s*[:\-]?\s*([\d,.]+)", re.I)
CHARGEABLE_PAT = re.compile(
    r"CH\.?\s*W\s*[:\-]?\s*([\d,.]+)\s*(KG|KGS?|LB|M3|CBM)", re.I
)
AIR_FRT_PAT = re.compile(
    r"AIR\s*FREIGHT(?:\s+(?:RATE|CHARGES?|COSTS?))?\s*"
    r"(?:[A-Z]{3}\s+)?([\d,]+\.\d{2})", re.I
)
SEA_FRT_PAT = re.compile(
    r"(?:SEA|OCEAN)\s*FREIGHT(?:\s+(?:RATE|CHARGES?|COSTS?))?\s*"
    r"(?:[A-Z]{3}\s+)?([\d,]+\.\d{2})", re.I
)
SUBTOTAL_PAT = re.compile(
    r"Sub-?Total\s*[:\-]?\s*(?:([A-Z]{3})\s+)?([\d,.]+)", re.I
)
CURRENCY_ANY = re.compile(r"\b(CAD|USD|EUR|GBP|AUD)\b", re.I)


# ─────────────────────────────────────────────────────────────
# PARSE ONE PDF (unchanged except for filename handling)
# ─────────────────────────────────────────────────────────────
def parse_invoice_pdf_bytes(data: bytes, filename: str) -> Optional[Dict[str, Any]]:
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)

        inv_date = None
        m = INVOICE_DATE_PAT.search(text)
        if m:
            inv_date = m.group(1).strip().upper()

        currency = "USD"
        m = SUBTOTAL_PAT.search(text)
        if m and m.group(1):
            currency = m.group(1).upper()
        else:
            m = CURRENCY_ANY.search(text)
            if m:
                currency = m.group(1).upper()

        shipper = None
        m = SHIPPER_PAT.search(text)
        if m:
            shipper = re.sub(r"\s+", " ", m.group(1).strip())

        pieces = w_kg = v_m3 = None
        m = ROW_PIECES_GW_VOL_PAT.search(text)
        if m:
            pieces, w, v = m.groups()
            pieces = int(pieces)
            w_kg = _f(w)
            v_m3 = _f(v)
        else:
            m = ROW_GW_VOL_PAT.search(text)
            if m:
                w, v = m.groups()
                w_kg = _f(w)
                v_m3 = _f(v)

            if pieces is None:
                m = PIECES_PAT.search(text)
                if m:
                    pieces = int(m.group(1))

            if w_kg is None:
                m = GW_PAT.search(text)
                if m:
                    w_kg = _f(m.group(1))

            if v_m3 is None:
                m = VOL_PAT.search(text)
                if m:
                    v_m3 = _f(m.group(1))

        c_kg = c_cbm = None
        m = CHARGEABLE_PAT.search(text)
        if m:
            val, unit = m.groups()
            val = _f(val)
            if unit.lower().startswith(("kg", "lb")):
                c_kg = _to_kg(val, unit)
            else:
                c_cbm = val

        subtotal = None
        m = SUBTOTAL_PAT.search(text)
        if m:
            subtotal = _f(m.group(2))

        f_mode = f_rate = None
        m = AIR_FRT_PAT.search(text)
        if m:
            f_mode, f_rate = "Air", _f(m.group(1))
        else:
            m = SEA_FRT_PAT.search(text)
            if m:
                f_mode, f_rate = "Sea", _f(m.group(1))

        return {
            "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),

            # 🔥 FIXED: Store ONLY the extracted invoice ID
            "Filename": filename,

            "Invoice_Date": inv_date,
            "Currency": currency,
            "Shipper": shipper,
            "Weight_KG": w_kg,
            "Volume_M3": v_m3,
            "Chargeable_KG": c_kg,
            "Chargeable_CBM": c_cbm,
            "Pieces": pieces,
            "Subtotal": subtotal,
            "Freight_Mode": f_mode,
            "Freight_Rate": f_rate,
        }
    except Exception:
        traceback.print_exc()
        return None


# ─────────────────────────────────────────────────────────────
# STREAMLIT UI
# ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Invoice Processor – A→Z (Streamlit)",
    page_icon="📄",
    layout="wide",
)

st.title("📄 Invoice Processor – A→Z")
st.caption(
    "Upload freight invoices → Extract Invoice Date, Shipper, Weight, Volume, "
    "Chargeable, Subtotal, Freight Mode & Rate → Download Excel summary."
)

uploads = st.file_uploader(
    "Upload PDF invoice files",
    type=["pdf"],
    accept_multiple_files=True,
    help="Drag & drop or browse invoices.",
)

MAX_MB = 25
too_big = False
if uploads:
    for f in uploads:
        if f.size > MAX_MB * 1024 * 1024:
            st.error(f"❌ {f.name} is larger than {MAX_MB} MB")
            too_big = True

extract_btn = st.button("Extract Invoices", type="primary", disabled=(not uploads or too_big))

if extract_btn and uploads and not too_big:
    rows: List[Dict[str, Any]] = []
    progress = st.progress(0)
    status = st.empty()

    total = len(uploads)
    for i, f in enumerate(uploads, start=1):
        status.write(f"Parsing: **{f.name}**")

        data = f.read()

        # 🔥 APPLY INVOICE ID CLEANING HERE
        invoice_id = extract_invoice_id(f.name)

        row = parse_invoice_pdf_bytes(data, filename=invoice_id)

        if row:
            rows.append(row)
        else:
            st.warning(f"⚠️ Could not extract data from {f.name}")

        progress.progress(i / total)

    if not rows:
        st.error("❌ No data extracted.")
    else:
        df = pd.DataFrame(rows)
        for col in HEADERS:
            if col not in df.columns:
                df[col] = None
        df = df[HEADERS]

        st.subheader("Preview Results")
        st.dataframe(df, use_container_width=True)

        # Build Excel in memory
        output = io.BytesIO()
        wb = Workbook()
        ws = wb.active
        ws.append(HEADERS)
        for _, r in df.iterrows():
            ws.append([r[h] for h in HEADERS])
        wb.save(output)
        output.seek(0)

        st.success(f"✅ Extraction complete! {len(rows)} invoices processed.")

        st.download_button(
            "⬇️ Download Excel",
            data=output,
            file_name="Invoice_Summary.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

