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
    monkeypatch.setattr(app_main.settings, "dataspace_vc_enabled", True)
    await app_main._validate_dataspace_config()


async def test_missing_organization_refuses_to_start(
    bind_rec, monkeypatch, _registry_says
):
    bind_rec("rec-a", organization="org-a")
    monkeypatch.setattr(app_main.settings, "dataspace_vc_enabled", True)
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
    monkeypatch.setattr(app_main.settings, "dataspace_vc_enabled", True)
    _registry_says(None)

    await app_main._validate_dataspace_config()


async def test_known_organization_starts(bind_rec, monkeypatch, _registry_says):
    bind_rec("rec-a", organization="org-a")
    monkeypatch.setattr(app_main.settings, "dataspace_vc_enabled", True)
    _registry_says(True)

    await app_main._validate_dataspace_config()


async def test_binding_ignored_when_vc_disabled(
    bind_rec, monkeypatch, _registry_says
):
    bind_rec("rec-a", organization="org-a")
    monkeypatch.setattr(app_main.settings, "dataspace_vc_enabled", False)
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
    monkeypatch.setattr(app_main.settings, "dataspace_vc_enabled", False)

    await app_main._validate_dataspace_config()


async def test_malformed_block_refuses_to_start(bind_rec):
    bind_rec("rec-a", organization="Not A Valid Alias")

    with pytest.raises(ValueError, match="lowercase alphanumeric"):
        await app_main._validate_dataspace_config()
