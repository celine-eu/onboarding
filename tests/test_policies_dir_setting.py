"""The policy bundle is located by `CELINE_POLICIES_DIR`, the platform's one name.

Until 2026-10-15 this service read `POLICIES_DIR` while celine-sdk read
`CELINE_POLICIES_POLICIES_DIR` and the infra chart set `CELINE_POLICIES_DIR`, so
none of the three agreed and every deployment ran on the default.
"""

from __future__ import annotations

import pytest

from celine.onboarding.config.settings import REPO_ROOT, Settings


@pytest.fixture()
def clean_env(monkeypatch):
    for name in ("CELINE_POLICIES_DIR", "POLICIES_DIR"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _settings() -> Settings:
    # No env files: the checkout's `.env` must not decide what a default is.
    return Settings(_env_file=None)


def test_the_default_is_the_repository_bundle(clean_env):
    assert _settings().policies_dir == str(REPO_ROOT / "policies")


def test_celine_policies_dir_locates_the_bundle(clean_env):
    clean_env.setenv("CELINE_POLICIES_DIR", "/srv/policies")
    assert _settings().policies_dir == "/srv/policies"


def test_the_unprefixed_name_is_no_longer_read(clean_env):
    clean_env.setenv("POLICIES_DIR", "/srv/stale")
    assert _settings().policies_dir == str(REPO_ROOT / "policies")
