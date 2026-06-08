from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class StorageResult:
    backend_name: str
    folder_url: str | None = None
    file_urls: dict[str, str] = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


@runtime_checkable
class StorageBackend(Protocol):
    @property
    def name(self) -> str: ...

    async def upload_submission(
        self,
        ref: str,
        pdf_bytes: bytes,
        documents: list[tuple[str, bytes, str]],
    ) -> StorageResult: ...
