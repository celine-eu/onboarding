"""The Keycloak organization alias is the admin console's tenancy key.

`rec_slug -> organization` is what lets the access policy answer "may this
operator review *this* community?". It is one identifier across the platform —
the owners.yaml id, the Keycloak organization alias, the identity-registry owner
id — so the manifest must not be able to state it twice, differently.
"""

from __future__ import annotations

import pytest

from celine.onboarding.services import template_service as ts


class TestResolution:
    def test_no_key_means_no_organization(self, seed_rec):
        seed_rec("plain")
        assert ts.organization_for("plain") == ""

    def test_explicit_key_wins(self, seed_rec):
        seed_rec("rec-a", organization="community-a")
        assert ts.organization_for("rec-a") == "community-a"

    def test_falls_back_to_the_dataspace_block(self, seed_rec):
        """The two are the same identifier, so a bound REC need not restate it."""
        seed_rec("rec-a", dataspace={"organization": "community-a"})
        assert ts.organization_for("rec-a") == "community-a"

    def test_agreeing_declarations_resolve(self, seed_rec):
        manifest = seed_rec(
            "rec-a",
            organization="community-a",
            dataspace={"organization": "community-a"},
        )
        ts.validate_organization(manifest, where="test")
        assert ts.organization_for("rec-a") == "community-a"

    def test_whitespace_is_stripped(self, seed_rec):
        seed_rec("rec-a", organization="  community-a  ")
        assert ts.organization_for("rec-a") == "community-a"

    def test_dataspace_block_that_is_not_a_mapping_is_ignored(self, seed_rec):
        """`validate_dataspace_block` is what rejects this; reading must not crash."""
        seed_rec("rec-a", dataspace="community-a")
        assert ts.organization_for("rec-a") == ""


class TestValidation:
    def test_absent_key_is_valid(self):
        """Optional: such a REC is administrable only by platform operators."""
        ts.validate_organization({"slug": "x"}, where="test")

    def test_disagreeing_declarations_are_refused(self):
        with pytest.raises(ValueError, match="disagree"):
            ts.validate_organization(
                {
                    "organization": "community-a",
                    "dataspace": {"organization": "community-b"},
                },
                where="test",
            )

    def test_empty_value_is_refused(self):
        """A blank reads as a `${VAR}` that failed to interpolate, not as "none"."""
        with pytest.raises(ValueError, match="present but empty"):
            ts.validate_organization({"organization": "  "}, where="test")

    def test_none_value_is_refused(self):
        with pytest.raises(ValueError, match="present but empty"):
            ts.validate_organization({"organization": None}, where="test")

    @pytest.mark.parametrize(
        "alias",
        [
            "Community-A",  # uppercase
            "community_a",  # underscore
            "-community",  # leading hyphen
            "community-",  # trailing hyphen
            "community a",  # space
            "comm.unity",  # dot
        ],
    )
    def test_invalid_aliases_are_refused(self, alias):
        with pytest.raises(ValueError, match="lowercase alphanumeric"):
            ts.validate_organization({"organization": alias}, where="test")

    @pytest.mark.parametrize("alias", ["a", "a1", "community-a", "a-b-c", "rec1"])
    def test_valid_aliases_are_accepted(self, alias):
        ts.validate_organization({"organization": alias}, where="test")

    def test_the_error_names_the_manifest(self):
        with pytest.raises(ValueError, match="templates/rec-a/manifest.yaml"):
            ts.validate_organization(
                {"organization": "NOPE"}, where="templates/rec-a/manifest.yaml"
            )


class TestReverseLookup:
    def test_groups_recs_by_organization(self, seed_rec):
        seed_rec("rec-a", organization="community-a")
        seed_rec("rec-b", organization="community-a")
        seed_rec("rec-c", organization="community-b")
        seed_rec("rec-d")  # no organization

        assert sorted(ts.recs_for_organization("community-a")) == ["rec-a", "rec-b"]
        assert ts.recs_for_organization("community-b") == ["rec-c"]

    def test_unknown_organization_owns_nothing(self, seed_rec):
        seed_rec("rec-a", organization="community-a")
        assert ts.recs_for_organization("community-z") == []

    def test_empty_alias_matches_nothing(self, seed_rec):
        """A REC with no organization must not be swept up by a caller with none.

        Otherwise an operator whose token carries no organization claim would
        resolve to every unbound community on the deployment.
        """
        seed_rec("rec-a")
        seed_rec("rec-b", organization="community-b")
        assert ts.recs_for_organization("") == []

    def test_dataspace_bound_recs_are_found(self, seed_rec):
        seed_rec("rec-a", dataspace={"organization": "community-a"})
        assert ts.recs_for_organization("community-a") == ["rec-a"]
