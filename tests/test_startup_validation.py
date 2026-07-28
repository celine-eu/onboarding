"""Startup refuses a dataspace misconfiguration a REC manager would otherwise hit.

Onboarding never creates dataspace state, so there is nothing to fall back to
when a binding points at an organisation that does not exist. The failure has to
surface at boot, where an operator is looking — not mid-review.

Dataspace integration stays optional per community: a REC with no `dataspace`
block runs the full wizard and provisions no identity. That is supported.
"""
from __future__ import annotations

import pytest

import celine.onboarding.main as app_main
from celine.onboarding.services import template_service as ts


@pytest.fixture()
def _vocab_configured(monkeypatch):
    monkeypatch.setattr(ts.settings, "ds_ns_url", "http://connector:30001")
    monkeypatch.setattr(ts.settings, "ds_connector_url", "")


@pytest.fixture()
def _registry_says(monkeypatch):
    """Stub the registry lookup: True known, False unknown, None unreachable."""

    def _set(answer):
        async def _exists(alias):
            return answer

        import celine.onboarding.services.dataspace_identity as di

        monkeypatch.setattr(di, "organization_exists", _exists)

    return _set


async def test_rec_without_a_block_starts(bind_rec, monkeypatch):
    bind_rec("plain")
    monkeypatch.setattr(app_main.settings, "dataspace_enabled", True)
    await app_main._validate_dataspace_config()


async def test_missing_organization_refuses_to_start(
    bind_rec, monkeypatch, _registry_says
):
    bind_rec("rec-a", organization="org-a")
    monkeypatch.setattr(app_main.settings, "dataspace_enabled", True)
    _registry_says(False)

    with pytest.raises(RuntimeError, match="does not exist"):
        await app_main._validate_dataspace_config()


async def test_unreachable_registry_does_not_block_boot(
    bind_rec, monkeypatch, _registry_says
):
    """A transient outage elsewhere must not become an outage here.

    "No such owner" is a configuration error worth refusing on; "I could not
    ask" is not. Coupling boot to another service's availability would turn
    every registry blip into a failure to start.
    """
    bind_rec("rec-a", organization="org-a")
    monkeypatch.setattr(app_main.settings, "dataspace_enabled", True)
    _registry_says(None)

    await app_main._validate_dataspace_config()


async def test_known_organization_starts(bind_rec, monkeypatch, _registry_says):
    bind_rec("rec-a", organization="org-a")
    monkeypatch.setattr(app_main.settings, "dataspace_enabled", True)
    _registry_says(True)

    await app_main._validate_dataspace_config()


async def test_binding_ignored_when_vc_disabled(
    bind_rec, monkeypatch, _registry_says
):
    bind_rec("rec-a", organization="org-a")
    monkeypatch.setattr(app_main.settings, "dataspace_enabled", False)
    _registry_says(False)

    await app_main._validate_dataspace_config()


# ── the offers vocabulary ─────────────────────────────────────────────────────


async def test_data_sharing_without_a_vocabulary_refuses_to_start(
    bind_rec, monkeypatch
):
    """Otherwise the sharing step vanishes and nobody is ever asked."""
    manifest = bind_rec("rec-a")
    manifest["consent"] = {"data_sharing": {"required": False}}
    monkeypatch.setattr(ts.settings, "ds_ns_url", "")
    monkeypatch.setattr(ts.settings, "ds_connector_url", "")

    with pytest.raises(RuntimeError, match="DS_NS_URL or DS_CONNECTOR_URL"):
        await app_main._validate_dataspace_config()


async def test_data_sharing_with_a_vocabulary_starts(
    bind_rec, monkeypatch, _vocab_configured
):
    manifest = bind_rec("rec-a")
    manifest["consent"] = {"data_sharing": {"required": False}}
    monkeypatch.setattr(app_main.settings, "dataspace_enabled", False)

    await app_main._validate_dataspace_config()


async def test_malformed_block_refuses_to_start(bind_rec):
    bind_rec("rec-a", organization="Not A Valid Alias")

    with pytest.raises(ValueError, match="lowercase alphanumeric"):
        await app_main._validate_dataspace_config()


# ── REC registry ──────────────────────────────────────────────────────────────


async def test_rec_registry_block_without_a_url_refuses_to_start(bind_rec, monkeypatch):
    """Approving would enable somebody invisible to every pipeline downstream."""
    manifest = bind_rec("rec-a")
    manifest["rec_registry"] = {"community": "rec-a", "area": "north"}
    monkeypatch.setattr(ts.settings, "rec_registry_url", "")

    with pytest.raises(RuntimeError, match="REC_REGISTRY_URL is required"):
        await app_main._validate_dataspace_config()


async def test_rec_registry_block_with_a_url_starts(bind_rec, monkeypatch):
    manifest = bind_rec("rec-a")
    manifest["rec_registry"] = {"community": "rec-a", "area": "north"}
    monkeypatch.setattr(ts.settings, "rec_registry_url", "http://registry:8004")
    monkeypatch.setattr(app_main.settings, "dataspace_enabled", False)

    await app_main._validate_dataspace_config()


async def test_an_sdk_without_the_write_wrapper_refuses_to_start(
    bind_rec, monkeypatch
):
    """The wrappers are unreleased, so `uv sync` alone installs an SDK that has
    the generated endpoints and not the wrapper. Better to say so at boot than to
    raise AttributeError the first time somebody is approved."""
    manifest = bind_rec("rec-a")
    manifest["rec_registry"] = {"community": "rec-a", "area": "north"}
    monkeypatch.setattr(ts.settings, "rec_registry_url", "http://registry:8004")

    from celine.sdk.rec_registry.client import RecRegistryAdminClient

    monkeypatch.delattr(RecRegistryAdminClient, "create_member", raising=False)

    with pytest.raises(RuntimeError, match="celine-sdk is too old"):
        await app_main._validate_dataspace_config()


async def test_no_rec_registry_block_ignores_the_sdk_version(bind_rec, monkeypatch):
    """A community that does not register members does not care."""
    bind_rec("rec-a")
    monkeypatch.setattr(ts.settings, "rec_registry_url", "")
    monkeypatch.setattr(app_main.settings, "dataspace_enabled", False)

    from celine.sdk.rec_registry.client import RecRegistryAdminClient

    monkeypatch.delattr(RecRegistryAdminClient, "create_member", raising=False)

    await app_main._validate_dataspace_config()
