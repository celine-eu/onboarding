"""The dataspace binding is per-REC, and lives in the template manifest.

It used to be a set of global environment variables, which meant a deployment
serving two communities filed every approved member into one dataspace
organisation — silently, since the wrong membership is still a successful 201.
The alias is one identifier across the platform: the owner id in the deployment's
owners.yaml, the Keycloak organization alias, and the identity-registry owner id.
"""
from __future__ import annotations

import pytest

from celine.onboarding.services import template_service as ts


def test_no_block_means_not_in_the_dataspace(bind_rec):
    bind_rec("plain")
    binding = ts.dataspace_binding("plain")
    assert binding.enabled is False
    assert binding.organization == ""


def test_block_resolves_every_field(bind_rec):
    bind_rec(
        "rec-a",
        organization="rec-a",
        organization_did="did:web:rec-a.dataspaces.localhost",
        linked_participant_did="did:web:consumer.dataspaces.localhost",
        membership_role="participant",
    )
    binding = ts.dataspace_binding("rec-a")
    assert binding.enabled is True
    assert binding.organization == "rec-a"
    assert binding.organization_did == "did:web:rec-a.dataspaces.localhost"
    assert binding.linked_participant_did == "did:web:consumer.dataspaces.localhost"
    assert binding.membership_role == "participant"


def test_membership_role_defaults(bind_rec):
    bind_rec("rec-a", organization="rec-a")
    assert ts.dataspace_binding("rec-a").membership_role == "member"


def test_two_recs_do_not_share_a_binding(bind_rec):
    bind_rec("rec-a", organization="org-a")
    bind_rec("rec-b", organization="org-b")
    assert ts.dataspace_binding("rec-a").organization == "org-a"
    assert ts.dataspace_binding("rec-b").organization == "org-b"


# ── validation, which runs at template import ─────────────────────────────────


def test_absent_block_validates():
    ts.validate_dataspace_block(None, where="test")


@pytest.mark.parametrize(
    "alias",
    ["rec-example", "greenland2", "a", "x-y-z"],
)
def test_accepts_aliases_the_owners_schema_accepts(alias):
    """Including the single-character form, which the owners schema permits.

    A value valid in owners.yaml must not be rejected here — they are the same
    identifier, and a divergence would defeat the point of having one.
    """
    ts.validate_dataspace_block({"organization": alias}, where="test")


@pytest.mark.parametrize(
    "alias",
    ["REC_Example!", "Rec-Example", "-leading", "trailing-", "with space"],
)
def test_rejects_malformed_aliases(alias):
    with pytest.raises(ValueError, match="lowercase alphanumeric"):
        ts.validate_dataspace_block({"organization": alias}, where="test")


def test_rejects_non_mapping():
    with pytest.raises(ValueError, match="must be a mapping"):
        ts.validate_dataspace_block(["rec-a"], where="test")


@pytest.mark.parametrize("key", ["organization_did", "linked_participant_did"])
def test_rejects_a_did_that_is_not_a_did(key):
    with pytest.raises(ValueError, match="must be a DID"):
        ts.validate_dataspace_block(
            {"organization": "rec-a", key: "https://rec-a.example"}, where="test"
        )


def test_block_without_organization_is_allowed_but_warned(bind_rec, caplog):
    """Legal — a credential with no membership — but almost always a mistake.

    The consent endpoints gate on membership, so such a member holds an identity
    that cannot do anything. Preserved rather than refused because it was the
    behaviour before the binding moved into the manifest.
    """
    with caplog.at_level("WARNING"):
        ts.validate_dataspace_block(
            {"linked_participant_did": "did:web:consumer.dataspaces.localhost"},
            where="test",
        )
    assert "no 'organization'" in caplog.text


# ── the deprecated global fallback ────────────────────────────────────────────


def test_env_fallback_applies_and_warns(bind_rec, monkeypatch, caplog):
    bind_rec("legacy")  # no dataspace block
    monkeypatch.setattr(ts.settings, "dataspace_organization_alias", "rec-legacy")
    monkeypatch.setattr(ts.settings, "dataspace_organization_did", "did:web:legacy")
    ts._env_fallback_warned.clear()

    with caplog.at_level("WARNING"):
        binding = ts.dataspace_binding("legacy")

    assert binding.organization == "rec-legacy"
    assert binding.organization_did == "did:web:legacy"
    assert "deprecated" in caplog.text


def test_manifest_wins_over_the_global(bind_rec, monkeypatch):
    bind_rec("rec-a", organization="from-manifest")
    monkeypatch.setattr(ts.settings, "dataspace_organization_alias", "from-env")
    assert ts.dataspace_binding("rec-a").organization == "from-manifest"


# ── the public config payload must not carry it ───────────────────────────────


def test_get_config_does_not_expose_the_binding(bind_rec):
    """`get_config` is a fixed allow-list and has to stay one.

    The payload is served unauthenticated to the wizard. The binding is not
    secret, but it has no business there — and the natural future refactor of
    that function is "just return the manifest".
    """
    bind_rec("rec-a", organization="rec-a", organization_did="did:web:rec-a")
    assert "dataspace" not in ts.get_config("rec-a")
