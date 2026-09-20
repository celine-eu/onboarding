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


def test_a_block_must_name_an_organization(bind_rec):
    """There is no "in the dataspace, but a member of nothing" state.

    A credential without a membership is an identity that cannot do anything —
    the consent endpoints gate on membership. Rather than let a manifest express
    it and warn, it is not expressible: omit the block to stay out.
    """
    bind_rec("rec-a", linked_participant_did="did:web:consumer.dataspaces.localhost")
    with pytest.raises(ValueError, match="'dataspace.organization' is required"):
        ts.dataspace_binding("rec-a")


def test_block_resolves_every_field(bind_rec):
    bind_rec(
        "rec-a",
        organization="rec-a",
        organization_did="did:web:rec-a.dataspaces.localhost",
        linked_participant_did="did:web:consumer.dataspaces.localhost",
    )
    binding = ts.dataspace_binding("rec-a")
    assert binding.enabled is True
    assert binding.organization == "rec-a"
    assert binding.organization_did == "did:web:rec-a.dataspaces.localhost"
    assert binding.linked_participant_did == "did:web:consumer.dataspaces.localhost"


def test_a_manifest_that_still_declares_a_membership_role_still_loads(bind_rec):
    """The key was removed; a manifest carrying it must not stop working.

    It named a role the registry stored in a column nothing read and has since
    dropped. Communities are not asked to re-cut their manifests for a value that
    never had an effect, so the key is ignored rather than rejected — and there is
    no attribute left for anything to read it back from.
    """
    bind_rec("rec-a", organization="rec-a", membership_role="participant")
    binding = ts.dataspace_binding("rec-a")
    assert binding.organization == "rec-a"
    assert not hasattr(binding, "membership_role")


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
    ["rec-example", "community2", "a", "x-y-z"],
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


def test_block_without_organization_is_refused():
    with pytest.raises(ValueError, match="'dataspace.organization' is required"):
        ts.validate_dataspace_block(
            {"linked_participant_did": "did:web:consumer.dataspaces.localhost"},
            where="test",
        )


def test_there_is_no_deployment_wide_binding():
    """The binding is per community, with no global fallback.

    A deployment-wide alias would file every community's members into one
    dataspace organisation — silently, since the wrong membership is still a
    successful 201. There is deliberately nothing to fall back to.
    """
    from celine.onboarding.config.settings import Settings

    fields = Settings.model_fields
    for removed in (
        "dataspace_organization_alias",
        "dataspace_organization_did",
        "dataspace_organization_name",
        "dataspace_linked_participant_did",
        "dataspace_membership_role",
    ):
        assert removed not in fields


# ── the public config payload must not carry it ───────────────────────────────


def test_get_config_does_not_expose_the_binding(bind_rec):
    """`get_config` is a fixed allow-list and has to stay one.

    The payload is served unauthenticated to the wizard. The binding is not
    secret, but it has no business there — and the natural future refactor of
    that function is "just return the manifest".
    """
    bind_rec("rec-a", organization="rec-a", organization_did="did:web:rec-a")
    assert "dataspace" not in ts.get_config("rec-a")


# ── per-offer connector routing ───────────────────────────────────────────────
#
# A consent is recorded at the connector that serves the data, which is not
# always the community's own: a grid operator holds its members' meter readings,
# and a decision to release them recorded anywhere else enforces nothing. Which
# offer goes where is configuration — never inferred from the offer — and the
# manifest is where it is written, per REC, like every other dataspace binding.


def test_no_connectors_block_keeps_every_offer_here(bind_rec):
    """The ordinary community: all of its data is its own."""
    bind_rec("rec-a", organization="example-rec")
    binding = ts.dataspace_binding("rec-a")
    assert binding.connectors == ()
    assert binding.connectors_for("meter-data-release") == ()
    assert binding.recorded_here("meter-data-release")


def test_a_routed_offer_resolves_to_the_holder(bind_rec):
    bind_rec(
        "rec-a",
        organization="example-rec",
        connectors=[
            {
                "holder": "example-dso",
                "url": "http://dso:30001/",
                "offers": ["meter-data-release", "forecasting-and-research"],
            }
        ],
    )
    binding = ts.dataspace_binding("rec-a")
    (connector,) = binding.connectors_for("meter-data-release")
    assert connector.holder == "example-dso"
    # The trailing slash is dropped once, here, so no caller has to think about it.
    assert connector.url == "http://dso:30001"
    assert binding.connectors_for("forecasting-and-research") == (connector,)
    # Routed elsewhere and not named by the community's own entry: not here.
    assert not binding.recorded_here("meter-data-release")
    # An offer nobody routed stays at the community's own connector.
    assert binding.connectors_for("household-energy-flexibility") == ()
    assert binding.recorded_here("household-energy-flexibility")


def test_two_recs_route_independently(bind_rec):
    """Multi-tenancy again: one community's routing is not another's."""
    bind_rec(
        "rec-a",
        organization="example-rec",
        connectors=[{"holder": "example-dso", "url": "http://dso:30001", "offers": ["release"]}],
    )
    bind_rec("rec-b", organization="other-rec")
    assert [c.holder for c in ts.dataspace_binding("rec-a").connectors_for("release")] == [
        "example-dso"
    ]
    assert ts.dataspace_binding("rec-b").connectors_for("release") == ()


def test_connectors_must_be_a_list():
    with pytest.raises(ValueError, match="'dataspace.connectors' must be a list"):
        ts.validate_dataspace_block(
            {"organization": "example-rec", "connectors": {"holder": "example-dso"}},
            where="test",
        )


def test_a_connector_must_name_its_holder():
    with pytest.raises(ValueError, match="has no 'holder'"):
        ts.validate_dataspace_block(
            {
                "organization": "example-rec",
                "connectors": [{"url": "http://dso:30001", "offers": ["release"]}],
            },
            where="test",
        )


def test_a_holder_is_an_owner_alias():
    with pytest.raises(ValueError, match="'holder' must be lowercase"):
        ts.validate_dataspace_block(
            {
                "organization": "example-rec",
                "connectors": [
                    {"holder": "Example DSO", "url": "http://dso:30001", "offers": ["release"]}
                ],
            },
            where="test",
        )


@pytest.mark.parametrize("url", ["", "dso:30001", "/consent"])
def test_a_connector_must_carry_a_base_url(url):
    with pytest.raises(ValueError, match="'url' must be the holder's connector base URL"):
        ts.validate_dataspace_block(
            {
                "organization": "example-rec",
                "connectors": [{"holder": "example-dso", "url": url, "offers": ["release"]}],
            },
            where="test",
        )


@pytest.mark.parametrize("offers", [None, [], "release", [""]])
def test_a_connector_must_route_at_least_one_offer(offers):
    entry = {"holder": "example-dso", "url": "http://dso:30001"}
    if offers is not None:
        entry["offers"] = offers
    with pytest.raises(ValueError, match="'offers'|must be an offer id"):
        ts.validate_dataspace_block(
            {"organization": "example-rec", "connectors": [entry]}, where="test"
        )


# ── one offer, several connectors (ADR-0007) ─────────────────────────────────
#
# An offer whose data sits in several places — a grid operator's readings and the
# community's own meter datasets — is recorded at every connector holding it. The
# manifest says so by naming the offer in each holder's entry, and names the
# community's own connector as an entry whose holder is the community itself.


def _both(**own):
    return {
        "organization": "example-rec",
        "connectors": [
            {"holder": "example-dso", "url": "http://dso:30001", "offers": ["release", "research"]},
            {"holder": "example-rec", "offers": ["research"], **own},
        ],
    }


def test_one_offer_may_be_held_by_the_community_and_another_participant(bind_rec):
    bind_rec("rec-a", **_both())
    binding = ts.dataspace_binding("rec-a")

    assert [c.holder for c in binding.connectors_for("research")] == ["example-dso"]
    assert binding.recorded_here("research")
    # The release is named only for the grid operator: its data is theirs alone.
    assert not binding.recorded_here("release")
    # The community's own entry is not "another participant's connector" — it is
    # never read as a holder and never sent keys.
    assert [c.holder for c in binding.connectors] == ["example-dso"]
    assert binding.own_offers == ("research",)


def test_one_offer_may_be_held_by_two_other_participants(bind_rec):
    bind_rec(
        "rec-a",
        organization="example-rec",
        connectors=[
            {"holder": "example-dso", "url": "http://dso:30001", "offers": ["research"]},
            {"holder": "other-dso", "url": "http://other:30001", "offers": ["research"]},
        ],
    )
    binding = ts.dataspace_binding("rec-a")
    assert [c.holder for c in binding.connectors_for("research")] == ["example-dso", "other-dso"]
    assert not binding.recorded_here("research")


def test_the_communitys_own_entry_carries_no_url():
    """Its address is DS_CONNECTOR_URL; a second home for it disagrees some day."""
    with pytest.raises(ValueError, match="is this community itself"):
        ts.validate_dataspace_block(_both(url="http://rec:30001"), where="test")


def test_an_existing_manifest_means_what_it_meant():
    """No own entry: every offer another participant holds is recorded there only."""
    block = {
        "organization": "example-rec",
        "connectors": [{"holder": "example-dso", "url": "http://dso:30001", "offers": ["release"]}],
    }
    ts.validate_dataspace_block(block, where="test")


def test_one_holder_has_one_entry():
    """Two entries for one participant are two answers to where its connector is."""
    with pytest.raises(ValueError, match="already has an entry"):
        ts.validate_dataspace_block(
            {
                "organization": "example-rec",
                "connectors": [
                    {"holder": "example-dso", "url": "http://dso:30001", "offers": ["release"]},
                    {"holder": "example-dso", "url": "http://dso:30002", "offers": ["research"]},
                ],
            },
            where="test",
        )


def test_an_offer_is_listed_once_per_holder():
    with pytest.raises(ValueError, match="listed twice"):
        ts.validate_dataspace_block(
            {
                "organization": "example-rec",
                "connectors": [
                    {
                        "holder": "example-dso",
                        "url": "http://dso:30001",
                        "offers": ["release", "release"],
                    },
                ],
            },
            where="test",
        )
