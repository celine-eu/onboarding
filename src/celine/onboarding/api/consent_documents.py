import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from celine.onboarding.services import template_service

router = APIRouter(prefix="/consent-documents", tags=["consent-documents"])

SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")


def _consent_dir() -> Path:
    return template_service.get_consent_dir()


def _load_meta(slug: str) -> dict:
    meta_files = list(_consent_dir().glob(f"{slug}.*"))
    json_file = next((f for f in meta_files if f.name.endswith(".json")), None)
    if not json_file:
        raise HTTPException(404, f"No metadata for '{slug}'")
    return json.loads(json_file.read_text(encoding="utf-8"))


def _find_document(slug: str) -> Path:
    meta = _load_meta(slug)
    filename = meta.get("filename", f"{slug}.pdf")
    doc_path = _consent_dir() / filename
    if not doc_path.exists():
        raise HTTPException(404, f"Document file not found for '{slug}'")
    return doc_path


@router.get("")
async def list_consent_documents():
    """List all available consent documents with metadata."""
    consent_dir = _consent_dir()
    if not consent_dir.exists():
        return []

    docs = []
    for meta_file in sorted(consent_dir.glob("*.json")):
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        docs.append(meta)
    return docs


@router.get("/{slug}/meta")
async def get_consent_meta(slug: str):
    """Get metadata for a consent document."""
    if not SLUG_PATTERN.match(slug):
        raise HTTPException(400, "Invalid slug")
    return _load_meta(slug)


@router.get("/{slug}")
async def download_consent_document(slug: str):
    """Download/preview a consent document."""
    if not SLUG_PATTERN.match(slug):
        raise HTTPException(400, "Invalid slug")

    meta = _load_meta(slug)
    doc_path = _find_document(slug)

    return FileResponse(
        doc_path,
        media_type=meta.get("mime_type", "application/pdf"),
        filename=meta.get("filename", f"{slug}.pdf"),
        headers={"Content-Disposition": f"inline; filename=\"{meta.get('filename', slug + '.pdf')}\""},
    )
