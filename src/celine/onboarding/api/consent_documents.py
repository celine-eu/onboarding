import json
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

from celine.onboarding.api.deps import valid_rec_slug
from celine.onboarding.services import template_service

router = APIRouter(prefix="/consent-documents", tags=["consent-documents"])

SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")


def _consent_dir(rec_slug: str) -> Path:
    return template_service.get_consent_dir(rec_slug)


def _load_meta(rec_slug: str, slug: str) -> dict:
    meta_files = list(_consent_dir(rec_slug).glob(f"{slug}.*"))
    json_file = next((f for f in meta_files if f.name.endswith(".json")), None)
    if not json_file:
        raise HTTPException(404, f"No metadata for '{slug}'")
    return json.loads(json_file.read_text(encoding="utf-8"))


def _find_document(rec_slug: str, slug: str) -> Path:
    meta = _load_meta(rec_slug, slug)
    filename = meta.get("filename", f"{slug}.pdf")
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    consent_dir = _consent_dir(rec_slug)
    doc_path = (consent_dir / filename).resolve()
    if not doc_path.is_relative_to(consent_dir.resolve()):
        raise HTTPException(400, "Invalid path")
    if not doc_path.exists():
        raise HTTPException(404, f"Document file not found for '{slug}'")
    return doc_path


@router.get("")
async def list_consent_documents(rec_slug: str = Depends(valid_rec_slug)):
    consent_dir = _consent_dir(rec_slug)
    if not consent_dir.exists():
        return []

    docs = []
    for meta_file in sorted(consent_dir.glob("*.json")):
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        docs.append(meta)
    return docs


@router.get("/{slug}/meta")
async def get_consent_meta(slug: str, rec_slug: str = Depends(valid_rec_slug)):
    if not SLUG_PATTERN.match(slug):
        raise HTTPException(400, "Invalid slug")
    return _load_meta(rec_slug, slug)


def _get_consent_url(rec_slug: str, slug: str) -> str | None:
    manifest = template_service.load_manifest(rec_slug)
    consent = manifest.get("consent", {}).get(slug, {})
    return consent.get("url")


@router.get("/{slug}")
async def download_consent_document(slug: str, rec_slug: str = Depends(valid_rec_slug)):
    if not SLUG_PATTERN.match(slug):
        raise HTTPException(400, "Invalid slug")

    url = _get_consent_url(rec_slug, slug)
    if url:
        return RedirectResponse(url)

    meta = _load_meta(rec_slug, slug)
    doc_path = _find_document(rec_slug, slug)

    return FileResponse(
        doc_path,
        media_type=meta.get("mime_type", "application/pdf"),
        filename=meta.get("filename", f"{slug}.pdf"),
        headers={"Content-Disposition": f"inline; filename=\"{meta.get('filename', slug + '.pdf')}\""},
    )
