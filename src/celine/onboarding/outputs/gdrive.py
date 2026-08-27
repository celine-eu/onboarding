from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path

from celine.onboarding.outputs.base import StorageResult
from celine.onboarding.outputs.registry import register_backend

logger = logging.getLogger(__name__)


class GDriveBackend:
    name = "gdrive"

    def __init__(
        self,
        folder_id: str,
        credentials_file: str,
        template_dir: str = "",
        **_kwargs: object,
    ) -> None:
        self._folder_id = folder_id
        creds_path = Path(credentials_file)
        if not creds_path.is_absolute() and template_dir:
            creds_path = Path(template_dir) / creds_path
        self._credentials_path = creds_path
        self._service = None

    def _get_service(self):
        if self._service is not None:
            return self._service

        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_service_account_file(
            str(self._credentials_path),
            scopes=["https://www.googleapis.com/auth/drive.file"],
        )
        self._service = build("drive", "v3", credentials=creds)
        return self._service

    async def upload_submission(
        self,
        ref: str,
        pdf_bytes: bytes,
        documents: list[tuple[str, bytes, str]],
    ) -> StorageResult:
        return await asyncio.to_thread(self._sync_upload, ref, pdf_bytes, documents)

    def _sync_upload(
        self,
        ref: str,
        pdf_bytes: bytes,
        documents: list[tuple[str, bytes, str]],
    ) -> StorageResult:
        from googleapiclient.http import MediaIoBaseUpload

        service = self._get_service()
        files = service.files()

        folder_meta = {
            "name": ref,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [self._folder_id],
        }
        folder = files.create(body=folder_meta, fields="id,webViewLink").execute()
        folder_id = folder["id"]
        folder_url = folder.get("webViewLink")

        file_urls: dict[str, str] = {}

        pdf_name = f"{ref}-summary.pdf"
        media = MediaIoBaseUpload(io.BytesIO(pdf_bytes), mimetype="application/pdf")
        pdf_file = files.create(
            body={"name": pdf_name, "parents": [folder_id]},
            media_body=media,
            fields="id,webViewLink",
        ).execute()
        file_urls[pdf_name] = pdf_file.get("webViewLink", "")

        for filename, content, mime_type in documents:
            media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type)
            doc_file = files.create(
                body={"name": filename, "parents": [folder_id]},
                media_body=media,
                fields="id,webViewLink",
            ).execute()
            file_urls[filename] = doc_file.get("webViewLink", "")

        return StorageResult(
            backend_name="gdrive",
            folder_url=folder_url,
            file_urls=file_urls,
            metadata={"folder_id": folder_id},
        )


register_backend("gdrive", GDriveBackend)
