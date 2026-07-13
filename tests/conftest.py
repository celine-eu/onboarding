from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _minimal_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")


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
