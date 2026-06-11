from __future__ import annotations

import logging
from urllib.parse import quote

from celine.onboarding.config.settings import settings
from celine.onboarding.models.submission import Submission
from celine.onboarding.outputs.base import StorageResult
from celine.onboarding.outputs.registry import get_backend
from celine.onboarding.outputs.webhook import fire_webhook, resolve_env
from celine.onboarding.services import template_service

logger = logging.getLogger(__name__)


def _collect_documents(submission: Submission) -> list[tuple[str, bytes, str]]:
    from celine.onboarding.services.document_service import read_file

    result = []
    for doc in submission.documents or []:
        try:
            content = read_file(doc)
            result.append((doc.original_filename, content, doc.mime_type))
        except Exception:
            logger.warning("Could not read document %s for upload", doc.id)
    return result


def _build_download_url(submission: Submission) -> str | None:
    from celine.onboarding.services.crypto import generate_download_token

    token = generate_download_token(submission.id)
    if token is None:
        return None
    base = notifications_base_url(submission.rec_slug)
    return f"{base}/api/downloads/{quote(token, safe='')}"


def notifications_base_url(rec_slug: str) -> str:
    manifest = template_service.load_manifest(rec_slug)
    notifications = manifest.get("notifications", {})
    base = notifications.get("base_url", "").rstrip("/")
    if base:
        return base
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    return origins[0] if origins else "http://localhost:8040"


async def handle_submission_notification(submission: Submission) -> None:
    manifest = template_service.load_manifest(submission.rec_slug)
    notifications = manifest.get("notifications", {})

    storage_result: StorageResult | None = None
    storage_config = notifications.get("storage")
    if storage_config:
        try:
            backend = get_backend(
                storage_config,
                str(template_service.template_dir_for(submission.rec_slug)),
            )
            if backend:
                from celine.onboarding.services.pdf_service import generate_submission_pdf

                pdf_bytes = generate_submission_pdf(submission)
                documents = _collect_documents(submission)
                storage_result = await backend.upload_submission(
                    submission.ref, pdf_bytes, documents
                )
                logger.info(
                    "Uploaded submission %s to %s: %s",
                    submission.ref,
                    storage_result.backend_name,
                    storage_result.folder_url,
                )
        except Exception:
            logger.exception("Storage backend upload failed for %s", submission.ref)

    download_url: str | None = None
    if storage_result and storage_result.folder_url:
        download_url = storage_result.folder_url
    else:
        download_url = _build_download_url(submission)

    email_enabled = notifications.get("email", True)
    if email_enabled and settings.smtp_host:
        try:
            from celine.onboarding.services.email_service import send_submission_email

            send_submission_email(submission, download_url=download_url)
        except Exception:
            logger.exception("Email notification failed for %s", submission.ref)

    webhook_config = notifications.get("webhook")
    if webhook_config:
        try:
            url = webhook_config["url"]
            raw_secret = webhook_config.get("secret", "")
            secret = resolve_env(raw_secret) if raw_secret else None
            payload = {
                "event": "submission.submitted",
                "submission_id": str(submission.id),
                "ref": submission.ref,
                "status": submission.status.value,
                "storage_url": storage_result.folder_url if storage_result else None,
            }
            await fire_webhook(url, payload, secret=secret or None)
        except Exception:
            logger.exception("Webhook dispatch failed for %s", submission.ref)
