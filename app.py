# app.py — Streamlit version of FINAL A→Z Invoice Processor
# Extracts: Reference, Commercial Value, Duties, GST/HST, Broker_Fee

import io
import re
import pdfplumber
import pandas as pd
import streamlit as st
from datetime import datetime

# Fixed column order
HEADERS = [
    "Timestamp", "Filename", "Reference",
    "Commercial_Value", "GST_HST", "Duties", "Broker_Fee"
]


# ---------------------------------------------------------
# Parse ONE PDF (from bytes)
# ---------------------------------------------------------
def parse_invoice_pdf_bytes(data: bytes, filename: str):
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pg1_text = pdf.pages[0].extract_text() or ""

            # -------------------------
            # Reference (starts with 13…)
            # -------------------------
            ref = None
            for pat in (
                r"Reference:\s*(13[\d-]+)",
                r"Customs Transaction:\s*(13[\d-]+)",
                r"Cargo Control Number:\s*(13[\d-]+)"
            ):
                m = re.search(pat, pg1_text)
                if m:
                    ref = m.group(1)
                    break

            # -------------------------
            # Broker Fee (Amount Due CAD …)
            # -------------------------
            fee = None
            fee_match = re.search(r"Amount\s+Due\s*:?\s*CAD\s*([\d,]+\.\d{2})", pg1_text)
            if fee_match:
                fee = float(fee_match.group(1).replace(",", ""))

            # ---------------------------------------------
            # Search all pages for Commercial Value, Duties, GST
            # ---------------------------------------------
            val = gst = dut = None

            for page in pdf.pages:
                txt = page.extract_text() or ""

                if val is None:
                    m = re.search(r"Value for Fee \(CDN\):\s*([\d,]+\.\d{2})", txt)
                    if m:
                        val = float(m.group(1).replace(",", ""))

                m1 = re.search(r"Duties\s*=\s*\$([\d,]+\.\d{2})", txt)
                if m1:
                    dut = float(m1.group(1).replace(",", ""))

                m2 = re.search(r"GST\s*=\s*\$([\d,]+\.\d{2})", txt)
                if m2:
                    gst = float(m2.group(1).replace(",", ""))

                if all(v is not None for v in (ref, val, gst, dut, fee)):
                    break

        return {
            "Timestamp": datetime.now(),
            "Filename": filename,
            "Reference": ref,
            "Commercial_Value": val,
            "GST_HST": gst,
            "Duties": dut,
            "Broker_Fee": fee,
        }

    except Exception:
        return None


# =========================================================
# Streamlit UI
# =========================================================

st.set_page_config(
    page_title="Invoice Processor – FINAL A→Z",
    page_icon="📄",
    layout="wide"
)

st.title("📄 Invoice Processor – FINAL A→Z (Broker Fee Included)")
st.caption("Upload broker invoice PDFs → Extract Reference, Duties, GST/HST, Commercial Value, Broker Fee → Get Excel Summary.")


uploads = st.file_uploader(
    "Upload PDF invoices",
    accept_multiple_files=True,
    type=["pdf"],
    help="Drag & drop or browse PDF broker invoices."
)

extract_btn = st.button("Extract Invoices", type="primary", disabled=(not uploads))


if extract_btn and uploads:
    rows = []
    progress = st.progress(0, text="Extracting…")
    status = st.empty()

    for i, f in enumerate(uploads, start=1):
        status.write(f"Processing: **{f.name}**")

        data = f.read()
        row = parse_invoice_pdf_bytes(data, filename=f.name)

        if row:
            rows.append(row)
        else:
            st.warning(f"⚠️ Nothing extracted from {f.name}")

        progress.progress(i / len(uploads))

    if not rows:
        st.error("❌ No valid data extracted.")
    else:
        df = pd.DataFrame(rows, columns=HEADERS)

        st.subheader("Preview")
        st.dataframe(df, use_container_width=True)

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Summary")

        output.seek(0)

        st.success(f"✅ Extraction complete. {len(rows)} file(s) processed.")

        st.download_button(
            label="⬇️ Download Excel Summary",
            data=output,
            file_name="Invoice_Summary.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
