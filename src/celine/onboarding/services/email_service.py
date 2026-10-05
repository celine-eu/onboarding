"""The emails a submission sends.

Two kinds, never mixed in one message:

- **The participant's receipt**: the reference, the status and the date. No link:
  the address is whatever the applicant typed, unverified, so nothing reachable
  from it may lead to their documents.
- **One message per operator**, addressed to that operator alone, with a link
  into the admin console. The console asks them to sign in, checks they review
  this community, and records every document they open. The email itself never
  carries the documents or a link that opens them.

A shared `To:` would show every operator's address to the participant and to
each other, and the participant would have received whatever the operators did.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from celine.onboarding.config.settings import settings
from celine.onboarding.models.submission import Submission
from celine.onboarding.services import template_service

logger = logging.getLogger(__name__)


def operator_recipients(rec_slug: str) -> list[str]:
    """The manifest's `notifications.notify`, else `SMTP_NOTIFY`."""
    notifications = template_service.load_manifest(rec_slug).get("notifications", {})
    notify_list = notifications.get("notify") or []
    if notify_list:
        return [str(a).strip() for a in notify_list if str(a).strip()]
    return [a.strip() for a in (settings.smtp_notify or "").split(",") if a.strip()]


def _summary(submission: Submission) -> str:
    date_str = (
        submission.created_at.strftime("%Y-%m-%d %H:%M:%S UTC") if submission.created_at else "-"
    )
    return (
        f"Submission reference: {submission.ref}\n"
        f"Status: {submission.status.value}\n"
        f"Date: {date_str}\n"
    )


def build_messages(submission: Submission, review_url: str | None = None) -> list[EmailMessage]:
    """The participant's receipt and one message per operator, each to one address."""
    manifest = template_service.load_manifest(submission.rec_slug)
    rec_name = manifest.get("name", "REC")
    notifications = manifest.get("notifications", {})
    from_addr = notifications.get("from") or settings.smtp_from or settings.smtp_user
    subject = f"{rec_name} — Submission {submission.ref}"
    summary = _summary(submission)

    messages: list[EmailMessage] = []

    if submission.email:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = submission.email
        msg.set_content(f"{summary}\nWe have received your application.\n")
        messages.append(msg)

    for operator in operator_recipients(submission.rec_slug):
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = operator
        body = summary
        if review_url:
            body += f"\nReview it in the admin console (sign-in required):\n{review_url}\n"
        msg.set_content(body)
        messages.append(msg)

    return messages


def send_submission_email(submission: Submission, review_url: str | None = None) -> None:
    if not settings.smtp_host:
        return

    messages = build_messages(submission, review_url=review_url)
    if not messages:
        return

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
        if settings.smtp_tls:
            ctx = ssl.create_default_context()
            server.starttls(context=ctx)
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        for msg in messages:
            server.send_message(msg)
