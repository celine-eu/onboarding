from pathlib import Path
from typing import Any

import yaml

from celine.onboarding.config.settings import REPO_ROOT, settings

_cache: dict[str, Any] | None = None


def _template_dir() -> Path:
    p = Path(settings.template_dir)
    if not p.is_absolute():
        p = REPO_ROOT / p
    return p


def load_manifest() -> dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache

    manifest_path = _template_dir() / "manifest.yaml"
    if not manifest_path.exists():
        _cache = _default_manifest()
        return _cache

    with open(manifest_path, encoding="utf-8") as f:
        _cache = yaml.safe_load(f)

    return _cache


def get_config() -> dict[str, Any]:
    manifest = load_manifest()
    return {
        "slug": manifest.get("slug", "default"),
        "name": manifest.get("name", "CER Onboarding"),
        "locale": manifest.get("locale", "it"),
        "branding": manifest.get("branding", {}),
        "fields": manifest.get("fields", {"extra": [], "hidden": []}),
        "consent": manifest.get("consent", {}),
        "steps": manifest.get("steps", ["consents", "utility", "personal", "statute", "review"]),
        "content": _load_content(manifest),
    }


def get_consent_dir() -> Path:
    tpl = _template_dir()
    consent_dir = tpl / "consent"
    if consent_dir.exists():
        return consent_dir
    return Path(settings.data_dir) / "consent"


def get_asset_path(relative: str) -> Path | None:
    path = _template_dir() / relative
    if path.exists() and path.is_file():
        return path
    return None


def _load_content(manifest: dict) -> dict[str, str]:
    content_map = manifest.get("content", {})
    tpl = _template_dir()
    result = {}
    for key, filename in content_map.items():
        path = tpl / filename
        if path.exists():
            result[key] = path.read_text(encoding="utf-8").strip()
    return result


def _default_manifest() -> dict[str, Any]:
    return {
        "slug": "default",
        "name": "CER Onboarding",
        "locale": "it",
        "branding": {},
        "fields": {"extra": [], "hidden": []},
        "consent": {
            "gdpr": {"version": "1.0", "required": True},
            "policy": {"version": "1.0", "required": True},
            "statute": {"version": "1.0", "required": True},
        },
        "steps": ["consents", "utility", "personal", "statute", "review"],
        "content": {},
    }


def reload():
    global _cache
    _cache = None
