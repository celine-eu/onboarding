"""The interactive API docs and the schema are mounted only in development.

`celine.sdk.posture.docs_urls`: in `CELINE_ENV=dev` `/docs`, `/redoc` and
`/openapi.json` are served; anywhere else, unset included, they are not mounted unless
`CELINE_PUBLIC_DOCS=true`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import celine.onboarding.main as app_main

PATHS = ("/docs", "/redoc", "/openapi.json")


def _client(monkeypatch, env: str | None, public: str | None = None) -> TestClient:
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    if env is None:
        monkeypatch.delenv("CELINE_ENV", raising=False)
    else:
        monkeypatch.setenv("CELINE_ENV", env)
    if public is None:
        monkeypatch.delenv("CELINE_PUBLIC_DOCS", raising=False)
    else:
        monkeypatch.setenv("CELINE_PUBLIC_DOCS", public)
    # No `with`: the lifespan (posture guard, database) is not under test here.
    return TestClient(app_main.create_app())


@pytest.mark.parametrize("env", [None, "staging", "prod"])
def test_outside_dev_the_docs_are_not_mounted(monkeypatch, env):
    """@verifies REQ-0028"""
    client = _client(monkeypatch, env)

    assert [client.get(p).status_code for p in PATHS] == [404, 404, 404]


@pytest.mark.parametrize("public", ["", "false", "0"])
def test_only_a_true_opt_in_serves_them_outside_dev(monkeypatch, public):
    """@verifies REQ-0028"""
    client = _client(monkeypatch, "staging", public)

    assert [client.get(p).status_code for p in PATHS] == [404, 404, 404]


def test_the_public_docs_opt_in_serves_them_outside_dev(monkeypatch):
    """@verifies REQ-0028"""
    client = _client(monkeypatch, "staging", "true")

    assert [client.get(p).status_code for p in PATHS] == [200, 200, 200]


def test_dev_serves_the_docs(monkeypatch):
    """@verifies REQ-0029"""
    client = _client(monkeypatch, "dev")

    assert [client.get(p).status_code for p in PATHS] == [200, 200, 200]
    assert client.get("/openapi.json").json()["info"]["title"] == "REC Onboarding"
