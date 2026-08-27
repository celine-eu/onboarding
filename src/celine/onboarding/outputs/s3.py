from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path

from celine.onboarding.outputs.base import StorageResult
from celine.onboarding.outputs.registry import register_backend
from celine.onboarding.outputs.webhook import resolve_env

logger = logging.getLogger(__name__)

DEFAULT_URL_EXPIRY = 604800  # 7 days


class S3Backend:
    name = "s3"

    def __init__(
        self,
        bucket: str,
        endpoint_url: str = "",
        access_key_id: str = "",
        secret_access_key: str = "",
        region: str = "",
        prefix: str = "",
        url_expiry_seconds: int = DEFAULT_URL_EXPIRY,
        env_file: str = "",
        template_dir: str = "",
        **_kwargs: object,
    ) -> None:
        self._load_template_env(env_file, template_dir)
        self._bucket = resolve_env(bucket)
        self._endpoint_url = resolve_env(endpoint_url) or None
        self._access_key_id = resolve_env(access_key_id)
        self._secret_access_key = resolve_env(secret_access_key)
        self._region = resolve_env(region) or None
        self._prefix = resolve_env(prefix).strip("/")
        self._url_expiry = int(url_expiry_seconds)
        self._client = None

    @staticmethod
    def _load_template_env(env_file: str, template_dir: str) -> None:
        if not env_file:
            return
        env_path = Path(env_file)
        if not env_path.is_absolute() and template_dir:
            env_path = Path(template_dir) / env_path
        if not env_path.is_file():
            return
        import os

        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            os.environ.setdefault(key, value)

    def _get_client(self):
        if self._client is not None:
            return self._client

        import boto3

        kwargs: dict = {}
        if self._endpoint_url:
            kwargs["endpoint_url"] = self._endpoint_url
        if self._region:
            kwargs["region_name"] = self._region
        if self._access_key_id:
            kwargs["aws_access_key_id"] = self._access_key_id
            kwargs["aws_secret_access_key"] = self._secret_access_key

        self._client = boto3.client("s3", **kwargs)
        return self._client

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
        client = self._get_client()
        folder_prefix = f"{self._prefix}/{ref}" if self._prefix else ref
        file_urls: dict[str, str] = {}

        pdf_key = f"{folder_prefix}/{ref}-summary.pdf"
        client.upload_fileobj(
            io.BytesIO(pdf_bytes),
            self._bucket,
            pdf_key,
            ExtraArgs={"ContentType": "application/pdf"},
        )
        file_urls[f"{ref}-summary.pdf"] = self._presign(client, pdf_key)

        for filename, content, mime_type in documents:
            key = f"{folder_prefix}/{filename}"
            client.upload_fileobj(
                io.BytesIO(content),
                self._bucket,
                key,
                ExtraArgs={"ContentType": mime_type},
            )
            file_urls[filename] = self._presign(client, key)

        folder_url = self._presign(client, pdf_key)

        return StorageResult(
            backend_name="s3",
            folder_url=folder_url,
            file_urls=file_urls,
            metadata={"bucket": self._bucket, "prefix": folder_prefix},
        )

    def _presign(self, client, key: str) -> str:
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=self._url_expiry,
        )


register_backend("s3", S3Backend)
