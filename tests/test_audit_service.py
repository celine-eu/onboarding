"""The audit trail names an actor, and commits with what it describes.

Both properties are the point of the module. An action nobody is recorded as
having taken is the gap a shared admin token left; an audit row committed
separately from its mutation is a trail that can disagree with the data.
"""

from __future__ import annotations

import pytest
from celine.sdk.auth import JwtUser

from celine.onboarding.models.audit_log import ACTOR_TYPES
from celine.onboarding.services import audit_service
from celine.onboarding.services.audit_service import Actor


class FakeSession:
    """Enough AsyncSession to observe what was staged and whether it committed."""

    def __init__(self) -> None:
        self.added: list = []
        self.commits = 0

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1


# ---------------------------------------------------------------------------
# Actor
# ---------------------------------------------------------------------------


class TestActor:
    def test_every_actor_type_is_declared(self):
        """The model documents the vocabulary; the constructors must stay inside it."""
        actors = [
            Actor.from_user(
                JwtUser(sub="s", email="op@example.org", claims={"email": "op@example.org"})
            ),
            Actor.local_cli(),
            Actor.system("scheduled-retry"),
            Actor.shared_token(),
        ]
        for actor in actors:
            assert actor.type in ACTOR_TYPES

    def test_operator_records_sub_and_email(self):
        user = JwtUser(
            sub="kc-user-uuid",
            email="operator@example.org",
            claims={
                "email": "operator@example.org",
                "preferred_username": "operator",
                "azp": "oauth2_proxy",
            },
        )
        actor = Actor.from_user(user)
        assert actor.type == "user"
        assert actor.sub == "kc-user-uuid"
        assert actor.email == "operator@example.org"
        # Which client the operator came through — console vs CLI with the same
        # identity.
        assert actor.client_id == "oauth2_proxy"

    def test_service_account_records_client_id_and_no_email(self):
        svc = JwtUser(
            sub="service-account-uuid",
            claims={
                "preferred_username": "service-account-svc-onboarding-cli",
                "client_id": "svc-onboarding-cli",
                "scope": "onboarding.admin",
            },
        )
        actor = Actor.from_user(svc)
        assert actor.type == "service"
        assert actor.client_id == "svc-onboarding-cli"
        assert actor.email is None

    def test_azp_is_used_when_client_id_is_absent(self):
        """Keycloak emits `azp`; some IdPs emit `client_id`. Accept either."""
        svc = JwtUser(
            sub="sa",
            claims={"preferred_username": "service-account-x", "azp": "svc-onboarding"},
        )
        assert Actor.from_user(svc).client_id == "svc-onboarding"

    def test_local_cli_claims_only_what_it_can(self):
        """No verified identity — the authority is shell access to the database."""
        actor = Actor.local_cli()
        assert actor.type == "cli"
        assert "@" in (actor.sub or "")
        assert actor.email is None
        assert actor.client_id is None

    def test_shared_token_has_no_identity_at_all(self):
        actor = Actor.shared_token()
        assert actor.type == "token"
        assert actor.sub is None
        assert actor.email is None
        assert actor.client_id is None

    def test_actors_are_immutable(self):
        with pytest.raises(Exception):
            Actor.shared_token().type = "user"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


class TestRecord:
    def test_stages_without_committing(self):
        """So the row and the change it describes commit together, or neither does.

        The previous helper committed one statement after the mutation had already
        committed; a crash in between left a change nobody was recorded as making.
        """
        db = FakeSession()
        audit_service.record(
            db,
            action="approve",
            entity_type="submission",
            entity_id="sub-1",
            actor=Actor.system("test"),
            rec_slug="my-rec",
        )
        assert len(db.added) == 1
        assert db.commits == 0

    def test_maps_every_field_onto_the_row(self):
        db = FakeSession()
        entry = audit_service.record(
            db,
            action="approve",
            entity_type="submission",
            entity_id="sub-1",
            actor=Actor(type="user", sub="kc-1", email="op@example.org", client_id="oauth2_proxy"),
            rec_slug="my-rec",
            ip="10.0.0.1",
            detail="status: under_review -> approved",
        )
        assert entry.action == "approve"
        assert entry.entity_type == "submission"
        assert entry.entity_id == "sub-1"
        assert entry.rec_slug == "my-rec"
        assert entry.ip_address == "10.0.0.1"
        assert entry.detail == "status: under_review -> approved"
        assert entry.actor_type == "user"
        assert entry.actor_sub == "kc-1"
        assert entry.actor_email == "op@example.org"
        assert entry.actor_client_id == "oauth2_proxy"

    def test_returns_the_row_so_a_caller_can_amend_it(self):
        db = FakeSession()
        entry = audit_service.record(
            db,
            action="retry",
            entity_type="submission",
            entity_id="sub-1",
            actor=Actor.shared_token(),
            rec_slug=None,
        )
        assert entry is db.added[0]

    async def test_record_and_commit_commits_once(self):
        """For an audited action that changes nothing itself — a read worth logging."""
        db = FakeSession()
        await audit_service.record_and_commit(
            db,
            action="reveal",
            entity_type="submission",
            entity_id="sub-1",
            actor=Actor.local_cli(),
            rec_slug="my-rec",
        )
        assert len(db.added) == 1
        assert db.commits == 1
