from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from celine.onboarding.config.settings import settings
from celine.onboarding.models.submission import Submission
from celine.onboarding.services import template_service

logger = logging.getLogger(__name__)


def send_submission_email(
    submission: Submission,
    download_url: str | None = None,
) -> None:
    if not settings.smtp_host:
        return

    manifest = template_service.load_manifest(submission.rec_slug)
    rec_name = manifest.get("name", "REC")
    notifications = manifest.get("notifications", {})

    recipients: list[str] = []
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
    date_str = (
        submission.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        if submission.created_at
        else "-"
    )

    msg = EmailMessage()
    msg["Subject"] = f"{rec_name} — Submission {submission.ref}"
    msg["From"] = from_addr
    msg["To"] = ", ".join(recipients)

    if download_url:
        body = (
            f"Submission reference: {submission.ref}\n"
            f"Status: {submission.status.value}\n"
            f"Date: {date_str}\n\n"
            f"Submission documents are available at:\n"
            f"{download_url}\n"
        )
    else:
        body = (
            f"Submission reference: {submission.ref}\n"
            f"Status: {submission.status.value}\n"
            f"Date: {date_str}\n"
        )
    msg.set_content(body)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
        if settings.smtp_tls:
            ctx = ssl.create_default_context()
            server.starttls(context=ctx)
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)
