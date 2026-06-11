import time
from pathlib import Path
from typing import Any

from celine.onboarding.config.settings import REPO_ROOT, settings

_cache: dict[str, dict[str, Any]] = {}
_cache_loaded_at: float = 0.0
CACHE_TTL: float = 5.0


async def load_recs_from_db() -> None:
    global _cache_loaded_at
    from sqlalchemy import select
    from celine.onboarding.models.database import async_session
    from celine.onboarding.models.rec import Rec

    async with async_session() as db:
        result = await db.execute(select(Rec).where(Rec.active == True))
        recs = result.scalars().all()
        _cache.clear()
        for rec in recs:
            _cache[rec.slug] = rec.manifest
    _cache_loaded_at = time.monotonic()


async def ensure_fresh() -> None:
    if time.monotonic() - _cache_loaded_at > CACHE_TTL:
        await load_recs_from_db()


def get_slugs() -> list[str]:
    return [slug for slug, manifest in _cache.items() if manifest]


def get_all_recs_summary() -> list[dict[str, Any]]:
    result = []
    for slug, manifest in _cache.items():
        if not manifest:
            continue
        result.append({
            "slug": slug,
            "name": manifest.get("name", slug),
            "locale": manifest.get("locale", "it"),
            "branding": manifest.get("branding", {}),
        })
    return result


def load_manifest(rec_slug: str) -> dict[str, Any]:
    if rec_slug not in _cache:
        raise KeyError(f"REC '{rec_slug}' not found")
    return _cache[rec_slug]


def _templates_dir() -> Path:
    p = Path(settings.templates_dir)
    if not p.is_absolute():
        p = REPO_ROOT / p
    return p


def template_dir_for(rec_slug: str) -> Path:
    return _templates_dir() / rec_slug


def get_config(rec_slug: str) -> dict[str, Any]:
    manifest = load_manifest(rec_slug)
    return {
        "slug": manifest.get("slug", rec_slug),
        "name": manifest.get("name", "CER Onboarding"),
        "locale": manifest.get("locale", "it"),
        "branding": manifest.get("branding", {}),
        "fields": manifest.get("fields", {"extra": [], "hidden": []}),
        "consent": manifest.get("consent", {}),
        "steps": manifest.get("steps", ["consents", "personal", "review"]),
        "content": _load_content(rec_slug, manifest),
    }


def get_consent_dir(rec_slug: str) -> Path:
    tpl = template_dir_for(rec_slug)
    consent_dir = tpl / "consent"
    if consent_dir.exists():
        return consent_dir
    return Path(settings.data_dir) / "consent"


def get_asset_path(rec_slug: str, relative: str) -> Path | None:
    if ".." in relative or relative.startswith("/"):
        return None
    tpl = template_dir_for(rec_slug)
    path = (tpl / relative).resolve()
    if not path.is_relative_to(tpl.resolve()):
        return None
    if path.exists() and path.is_file():
        return path
    return None


def _load_content(rec_slug: str, manifest: dict) -> dict[str, str]:
    content_map = manifest.get("content", {})
    tpl = template_dir_for(rec_slug)
    result = {}
    for key, filename in content_map.items():
        path = tpl / filename
        if path.exists():
            result[key] = path.read_text(encoding="utf-8").strip()
    return result


async def reload() -> None:
    await load_recs_from_db()
