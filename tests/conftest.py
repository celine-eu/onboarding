from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _minimal_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")


@pytest.fixture(autouse=True)
def _isolate_manifest_cache():
    """Keep the REC manifest cache from leaking between tests.

    The cache is module-level and TTL-refreshed from the database, so a test that
    seeds it would otherwise decide what a later test resolves.
    """
    from celine.onboarding.services import template_service

    saved = dict(template_service._cache)
    saved_at = template_service._cache_loaded_at
    yield
    template_service._cache.clear()
    template_service._cache.update(saved)
    template_service._cache_loaded_at = saved_at


@pytest.fixture()
def bind_rec(monkeypatch):
    """Seed a REC manifest so `dataspace_binding` resolves without a database.

    Returns a callable so a test can bind several communities and assert they do
    not bleed into one another — the multi-tenancy property the manifest binding
    exists to provide.
    """
    from celine.onboarding.services import template_service

    async def _noop_fresh():
        return None

    monkeypatch.setattr(template_service, "ensure_fresh", _noop_fresh)

    def _bind(rec_slug: str = "default", **dataspace):
        manifest: dict = {"slug": rec_slug, "name": rec_slug}
        if dataspace:
            manifest["dataspace"] = dataspace
        template_service._cache[rec_slug] = manifest
        return manifest

    return _bind


@pytest.fixture()
def seed_rec(monkeypatch):
    """Seed an arbitrary REC manifest into the cache, no database involved.

    `bind_rec` only reaches the `dataspace:` block; this one takes any top-level
    manifest key, which the admin console needs (`organization:`).
    """
    from celine.onboarding.services import template_service

    async def _noop_fresh():
        return None

    monkeypatch.setattr(template_service, "ensure_fresh", _noop_fresh)

    def _seed(rec_slug: str = "default", **manifest):
        full: dict = {"slug": rec_slug, "name": rec_slug, **manifest}
        template_service._cache[rec_slug] = full
        return full

    return _seed


@pytest.fixture()
def submission():
    sub = MagicMock()
    sub.ref = "20260713-abcd1234"
    sub.email = "user@example.com"
    sub.first_name = "Alice"
    sub.last_name = "Test"
    sub.rec_slug = "default"
    sub.dataspace_vc_id = None
    sub.dataspace_subject_id = None
    sub.dataspace_did = None
    sub.dataspace_vc_issued_at = None
    return sub
