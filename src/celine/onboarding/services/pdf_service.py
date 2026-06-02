import io
from datetime import datetime, timezone

from fpdf import FPDF

from celine.onboarding.models.submission import Submission
from celine.onboarding.services.template_service import load_manifest


def generate_submission_pdf(submission: Submission) -> bytes:
    manifest = load_manifest()
    rec_name = manifest.get("name", "CER Onboarding")

    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=20)

    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 12, rec_name, new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 6, f"Submission ref: {submission.ref}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(
        0, 6,
        f"Date: {submission.created_at.strftime('%Y-%m-%d %H:%M:%S UTC') if submission.created_at else '-'}",
        new_x="LMARGIN", new_y="NEXT",
    )
    pdf.cell(0, 6, f"Status: {submission.status.value}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    pdf.set_text_color(0, 0, 0)

    _section(pdf, "Personal Data")
    _row(pdf, "First name", submission.first_name)
    _row(pdf, "Last name", submission.last_name)
    _row(pdf, "Email", submission.email)
    _row(pdf, "Phone", submission.phone)
    _row(pdf, "Fiscal code", submission.fiscal_code)
    _row(pdf, "POD code", submission.pod_code)
    pdf.ln(4)

    _section(pdf, "Consents")
    _consent_row(pdf, "GDPR", submission.gdpr_consent, submission.gdpr_consent_at, submission.gdpr_consent_version)
    _consent_row(pdf, "Policy", submission.policy_consent, submission.policy_consent_at, submission.policy_consent_version)
    _consent_row(pdf, "Statute", submission.statute_consent, submission.statute_consent_at, submission.statute_consent_version)
    _row(pdf, "Keep me updated", "Yes" if submission.keep_me_updated else "No")
    _row(pdf, "Consent IP", submission.consent_ip)
    pdf.ln(4)

    if submission.extracted_data:
        _section(pdf, "Extracted Data (from bill)")
        for key, value in submission.extracted_data.items():
            _row(pdf, key, str(value) if value else None)
        pdf.ln(4)

    if submission.documents:
        _section(pdf, "Documents")
        for doc in submission.documents:
            _row(pdf, doc.doc_type.value, f"{doc.original_filename} ({doc.size_bytes} bytes)")
        pdf.ln(4)

    pdf.ln(8)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(150, 150, 150)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    pdf.cell(0, 5, f"Generated: {now} | Ref: {submission.ref}", new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())


def _section(pdf: FPDF, title: str):
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_fill_color(240, 242, 245)
    pdf.cell(0, 8, f"  {title}", new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)


def _row(pdf: FPDF, label: str, value: str | None):
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(55, 6, label, new_x="RIGHT")
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "B" if value else "", 9)
    pdf.cell(0, 6, value or "-", new_x="LMARGIN", new_y="NEXT")


def _consent_row(pdf: FPDF, label: str, accepted: bool, at: datetime | None, version: str | None):
    status = "Accepted" if accepted else "Not accepted"
    detail = status
    if at:
        detail += f" on {at.strftime('%Y-%m-%d %H:%M:%S')}"
    if version:
        detail += f" (v{version})"
    _row(pdf, label, detail)
