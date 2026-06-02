import io
import smtplib
import zipfile
from email.message import EmailMessage
from pathlib import Path

from celine.onboarding.config.settings import settings
from celine.onboarding.models.submission import Submission
from celine.onboarding.services.pdf_service import generate_submission_pdf
from celine.onboarding.services.template_service import load_manifest


def send_submission_email(submission: Submission) -> None:
    if not settings.smtp_host:
        return

    manifest = load_manifest()
    rec_name = manifest.get("name", "CER")
    notifications = manifest.get("notifications", {})

    pdf_bytes = generate_submission_pdf(submission)
    zip_bytes = _zip_documents(submission)

    recipients = []
    if submission.email:
        recipients.append(submission.email)
    notify_list = notifications.get("notify", [])
    if notify_list:
        recipients.extend(notify_list)
    elif settings.smtp_notify:
        recipients.extend(a.strip() for a in settings.smtp_notify.split(",") if a.strip())
    if not recipients:
        return

    from_addr = notifications.get("from") or settings.smtp_from or settings.smtp_user

    msg = EmailMessage()
    msg["Subject"] = f"{rec_name} — Submission {submission.ref}"
    msg["From"] = from_addr
    msg["To"] = ", ".join(recipients)

    body = (
        f"Submission reference: {submission.ref}\n"
        f"Name: {submission.first_name} {submission.last_name}\n"
        f"Status: {submission.status.value}\n"
        f"Date: {submission.created_at.strftime('%Y-%m-%d %H:%M:%S UTC') if submission.created_at else '-'}\n\n"
        f"Please find attached the submission summary (PDF) and uploaded documents (ZIP).\n"
    )
    msg.set_content(body)

    msg.add_attachment(
        pdf_bytes,
        maintype="application",
        subtype="pdf",
        filename=f"{submission.ref}-summary.pdf",
    )

    if zip_bytes:
        msg.add_attachment(
            zip_bytes,
            maintype="application",
            subtype="zip",
            filename=f"{submission.ref}-documents.zip",
        )

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
        if settings.smtp_tls:
            server.starttls()
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)


def _zip_documents(submission: Submission) -> bytes | None:
    if not submission.documents:
        return None

    data_dir = Path(settings.data_dir)
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for doc in submission.documents:
            file_path = data_dir / doc.file_path
            if file_path.exists():
                zf.write(file_path, doc.original_filename)

    result = buf.getvalue()
    return result if len(result) > 22 else None  # empty ZIP is 22 bytes
