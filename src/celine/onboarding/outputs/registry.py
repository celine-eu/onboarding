from __future__ import annotations

import logging
from typing import Any

from celine.onboarding.outputs.base import StorageBackend

logger = logging.getLogger(__name__)

_BACKENDS: dict[str, type] = {}


def register_backend(name: str, cls: type) -> None:
    _BACKENDS[name] = cls


def get_backend(config: dict[str, Any], template_dir: str) -> StorageBackend | None:
    _load_builtin_backends()
    backend_name = config.get("backend")
    if not backend_name:
        return None
    cls = _BACKENDS.get(backend_name)
    if cls is None:
        logger.warning("Unknown storage backend: %s", backend_name)
        return None
    return cls(template_dir=template_dir, **{k: v for k, v in config.items() if k != "backend"})


def _load_builtin_backends() -> None:
    if _BACKENDS:
        return
    try:
        import celine.onboarding.outputs.gdrive  # noqa: F401
    except Exception:
        pass
    try:
        import celine.onboarding.outputs.s3  # noqa: F401
    except Exception:
        pass
