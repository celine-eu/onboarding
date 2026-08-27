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
    """Stub the registry lookup: True known, False unknown, None unreachable.

    ``status`` is the owner's lifecycle state where one is known. Defaulting it
    to ``verified`` keeps every pre-existing caller meaning what it meant: these
    tests were written when existing and admissible were the same thing.
    """

    import celine.onboarding.services.dataspace_identity as di

    def _set(answer, status="verified"):
        async def _check(alias):
            return di.OwnerCheck(found=answer, status=status if answer else None)

        monkeypatch.setattr(di, "check_organization", _check)

    return _set


async def test_rec_without_a_block_starts(bind_rec, monkeypatch):
    bind_rec("plain")
    monkeypatch.setattr(app_main.settings, "dataspace_enabled", True)
    await app_main._validate_dataspace_config()


async def test_missing_organization_refuses_to_start(bind_rec, monkeypatch, _registry_says):
    bind_rec("rec-a", organization="org-a")
    monkeypatch.setattr(app_main.settings, "dataspace_enabled", True)
    _registry_says(False)

    with pytest.raises(RuntimeError, match="does not exist"):
        await app_main._validate_dataspace_config()


async def test_unreachable_registry_does_not_block_boot(bind_rec, monkeypatch, _registry_says):
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


async def test_binding_ignored_when_vc_disabled(bind_rec, monkeypatch, _registry_says):
    bind_rec("rec-a", organization="org-a")
    monkeypatch.setattr(app_main.settings, "dataspace_enabled", False)
    _registry_says(False)

    await app_main._validate_dataspace_config()


# ── the offers vocabulary ─────────────────────────────────────────────────────


async def test_data_sharing_without_a_vocabulary_refuses_to_start(bind_rec, monkeypatch):
    """Otherwise the sharing step vanishes and nobody is ever asked."""
    manifest = bind_rec("rec-a")
    manifest["consent"] = {"data_sharing": {"required": False}}
    monkeypatch.setattr(ts.settings, "ds_ns_url", "")
    monkeypatch.setattr(ts.settings, "ds_connector_url", "")

    with pytest.raises(RuntimeError, match="DS_NS_URL or DS_CONNECTOR_URL"):
        await app_main._validate_dataspace_config()


async def test_data_sharing_with_a_vocabulary_starts(bind_rec, monkeypatch, _vocab_configured):
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
    manifest["rec_registry"] = {"community": "rec-a", "default_area": "north"}
    monkeypatch.setattr(ts.settings, "rec_registry_url", "")

    with pytest.raises(RuntimeError, match="REC_REGISTRY_URL is required"):
        await app_main._validate_dataspace_config()


async def test_rec_registry_block_with_a_url_starts(bind_rec, monkeypatch):
    manifest = bind_rec("rec-a")
    manifest["rec_registry"] = {"community": "rec-a", "default_area": "north"}
    monkeypatch.setattr(ts.settings, "rec_registry_url", "http://registry:8004")
    monkeypatch.setattr(app_main.settings, "dataspace_enabled", False)

    await app_main._validate_dataspace_config()


# ---------------------------------------------------------------------------
# Organisation — the admin console's tenancy key
# ---------------------------------------------------------------------------


async def test_contradictory_organization_refuses_to_start(seed_rec, monkeypatch):
    """Manifests come from the database, so `import-templates` is not the only gate.

    A REC imported by an older build can carry a manifest this one considers
    invalid, and an operator authenticating against one alias while their members
    are filed under another is exactly the drift the single-identifier rule
    exists to prevent.
    """
    seed_rec(
        "rec-a",
        organization="community-a",
        dataspace={"organization": "community-b"},
    )
    monkeypatch.setattr(app_main.settings, "dataspace_enabled", False)

    with pytest.raises(ValueError, match="disagree"):
        await app_main._validate_dataspace_config()


async def test_malformed_organization_refuses_to_start(seed_rec, monkeypatch):
    seed_rec("rec-a", organization="Community_A")
    monkeypatch.setattr(app_main.settings, "dataspace_enabled", False)

    with pytest.raises(ValueError, match="lowercase alphanumeric"):
        await app_main._validate_dataspace_config()


async def test_rec_without_an_organization_warns_but_starts(seed_rec, monkeypatch, caplog):
    """Not fatal: platform operators with a realm group can still run it.

    But per-community delegation is impossible, and being told at boot beats
    finding out by being denied.
    """
    seed_rec("rec-a")
    monkeypatch.setattr(app_main.settings, "dataspace_enabled", False)

    with caplog.at_level("WARNING"):
        await app_main._validate_dataspace_config()

    assert "declares no 'organization'" in caplog.text


async def test_rec_with_an_organization_does_not_warn(seed_rec, monkeypatch, caplog):
    seed_rec("rec-a", organization="community-a")
    monkeypatch.setattr(app_main.settings, "dataspace_enabled", False)

    with caplog.at_level("WARNING"):
        await app_main._validate_dataspace_config()

    assert "declares no 'organization'" not in caplog.text


# ---------------------------------------------------------------------------
# The admin console must not appear protected when it is not
# ---------------------------------------------------------------------------


@pytest.fixture()
def _oidc_configured(monkeypatch):
    monkeypatch.setattr(app_main.settings, "oidc_base_url", "http://keycloak.test/realms/celine")
    monkeypatch.setattr(app_main.settings, "oidc_jwks_uri", "")
    monkeypatch.setattr(app_main.settings, "removed_admin_token", "")
    monkeypatch.setattr(app_main.settings, "allow_permissive_policy", False)
    from celine.onboarding.security import oidc

    oidc.oidc_settings.cache_clear()
    yield
    oidc.oidc_settings.cache_clear()


def test_leftover_admin_token_refuses_to_start(monkeypatch, _oidc_configured):
    """A variable that no longer does anything reads as protection that is not there."""
    monkeypatch.setattr(app_main.settings, "removed_admin_token", "leftover-secret")

    with pytest.raises(RuntimeError, match="no longer does anything"):
        app_main._validate_admin_config()


def test_unconfigured_oidc_refuses_to_start(monkeypatch, _oidc_configured):
    monkeypatch.setattr(app_main.settings, "oidc_base_url", "")
    from celine.onboarding.security import oidc

    oidc.oidc_settings.cache_clear()

    with pytest.raises(RuntimeError, match="OIDC_BASE_URL is required"):
        app_main._validate_admin_config()


def test_jwks_uri_is_derived_from_the_issuer(_oidc_configured):
    """Set only OIDC_BASE_URL and the realm's certs endpoint is assumed."""
    from celine.onboarding.security.oidc import oidc_settings

    assert oidc_settings().jwks_uri == (
        "http://keycloak.test/realms/celine/protocol/openid-connect/certs"
    )


def test_unloadable_policies_refuse_to_start(monkeypatch, _oidc_configured, tmp_path):
    from celine.onboarding.security import policy as policy_module

    monkeypatch.setattr(app_main.settings, "policies_dir", str(tmp_path / "absent"))
    policy_module.get_policy.cache_clear()

    try:
        with pytest.raises(RuntimeError, match="Access policies could not be loaded"):
            app_main._validate_admin_config()
    finally:
        policy_module.get_policy.cache_clear()


def test_permissive_flag_allows_boot_without_policies(
    monkeypatch, _oidc_configured, tmp_path, caplog
):
    from celine.onboarding.security import policy as policy_module

    monkeypatch.setattr(app_main.settings, "policies_dir", str(tmp_path / "absent"))
    monkeypatch.setattr(app_main.settings, "allow_permissive_policy", True)
    policy_module.get_policy.cache_clear()

    try:
        with caplog.at_level("WARNING"):
            app_main._validate_admin_config()
        assert "ALLOW_PERMISSIVE_POLICY" in caplog.text
    finally:
        policy_module.get_policy.cache_clear()


def test_reserved_rec_slug_refuses_to_start(seed_rec, _oidc_configured):
    """A REC named `recs` could never be addressed under /api/admin."""
    seed_rec("recs", organization="community-a")

    with pytest.raises(RuntimeError, match="reserved by the admin API"):
        app_main._validate_admin_config()


def test_ordinary_slugs_start_fine(seed_rec, _oidc_configured):
    seed_rec("rec-a", organization="community-a")
    app_main._validate_admin_config()


async def test_a_suspended_organization_refuses_to_start(bind_rec, monkeypatch, _registry_says):
    """Existing is not admissible.

    The registry gained verified/suspended/revoked in August and this check only
    ever asked whether the row was there, so a suspended owner kept taking new
    members. Its own enrolment service refuses to issue a token for one.
    """
    bind_rec("rec-a", organization="org-a")
    monkeypatch.setattr(app_main.settings, "dataspace_enabled", True)
    _registry_says(True, status="suspended")

    with pytest.raises(RuntimeError, match="is suspended"):
        await app_main._validate_dataspace_config()


async def test_a_revoked_organization_refuses_to_start(bind_rec, monkeypatch, _registry_says):
    bind_rec("rec-a", organization="org-a")
    monkeypatch.setattr(app_main.settings, "dataspace_enabled", True)
    _registry_says(True, status="revoked")

    with pytest.raises(RuntimeError, match="is revoked"):
        await app_main._validate_dataspace_config()


async def test_a_verified_organization_starts(bind_rec, monkeypatch, _registry_says):
    bind_rec("rec-a", organization="org-a")
    monkeypatch.setattr(app_main.settings, "dataspace_enabled", True)
    _registry_says(True, status="verified")

    await app_main._validate_dataspace_config()


async def test_a_registry_reporting_no_status_still_starts(bind_rec, monkeypatch, _registry_says):
    """An absent field must not read as "not verified".

    A registry that predates the lifecycle, or a body that could not be parsed,
    leaves the status unknown — and refusing on unknown would turn this check
    into the outage it was written to avoid.
    """
    bind_rec("rec-a", organization="org-a")
    monkeypatch.setattr(app_main.settings, "dataspace_enabled", True)
    _registry_says(True, status=None)

    await app_main._validate_dataspace_config()
