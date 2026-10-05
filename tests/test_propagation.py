"""A correction after approval reaches every system holding a copy, step by step.

Names go to the Keycloak account (through the provisioning service) and to the
REC registry member; an email goes to the account — whose username never changes
— then, for a member who never set a password, a fresh invitation, then the
identity registry's mapping, same DID. Each step is a row with its state, its
attempts and an error code; nothing on a row is a value.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from celine.sdk.openapi.provisioning.schemas import ParticipantUpdateResponseSchema
from celine.sdk.provisioning import ProvisioningApiError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_revision import FakeDb, make_submission

from celine.onboarding.api.admin import create_admin_router
from celine.onboarding.config.settings import settings
from celine.onboarding.models.enablement import EnablementStatus
from celine.onboarding.models.submission import SubmissionStatus
from celine.onboarding.security.middleware import AdminAuthMiddleware
from celine.onboarding.services import (
    audit_service,
    dataspace_identity,
    enablement,
    propagation,
    provisioning,
    rec_registry,
    revision,
    template_service,
)
from celine.onboarding.services.audit_service import Actor
from celine.onboarding.services.revision import RevisableField, RevisionMethod

ORG = "community-a"
OPERATOR = Actor(type="user", sub="op-1", email="operator@example.org")
COMMUNITY = "example-rec"
KC_USER = "kc-uuid-0001"
DID = "did:web:users.example:0a1b2c3d"
USERNAME = "ada@example.org"
OLD_EMAIL = "ada@example.org"
NEW_EMAIL = "ada.lovelace@example.org"

_OriginalAsyncClient = httpx.AsyncClient


def approved():
    submission = make_submission(SubmissionStatus.APPROVED)
    submission.dataspace_did = DID
    return submission


# ── fakes ────────────────────────────────────────────────────────────────────


class FakeProvisioning:
    """The SDK's `ProvisioningClient`, as far as propagation uses it."""

    def __init__(self):
        self.updates: list[dict] = []
        self.invitations: list[tuple] = []
        self.update_error: ProvisioningApiError | None = None
        self.invitation_code: str | Exception = "sent"
        self.account = {"first_name": "Ada", "last_name": "Lovelace", "email": OLD_EMAIL}

    async def update_participant(self, community, key, **fields):
        sent = {k: v for k, v in fields.items() if v is not None}
        self.updates.append({"community": community, "key": key, **sent})
        if self.update_error is not None:
            raise self.update_error
        changed = [k for k, v in sent.items() if self.account.get(k) != v]
        self.account.update(sent)
        return ParticipantUpdateResponseSchema(
            changed=changed,
            email=self.account["email"],
            email_verified="email" not in changed,
            first_name=self.account["first_name"],
            last_name=self.account["last_name"],
            user_id=KC_USER,
            # Never renamed, whatever the address becomes.
            username=USERNAME,
            verification="sent" if "email" in changed else "not_requested",
        )

    async def send_invitation(self, community, key, *, intent):
        self.invitations.append((community, key, intent, self.account["email"]))
        code = self.invitation_code
        if isinstance(code, Exception):
            raise code
        if code in provisioning.INVITATION_REFUSALS:
            raise ProvisioningApiError("refused", status_code=409, code=code)
        return SimpleNamespace(invitation=SimpleNamespace(value=code))


class FakeRegistry:
    """The registry calls propagation and the POD export make, over one member."""

    def __init__(self):
        self.patches: list[tuple] = []
        self.status = 200
        #: member key -> its delivery point ids, as the registry holds them.
        self.points: dict[str, list[str]] = {}
        #: member key -> meters, each `{"id", "pod"}`.
        self.meters: dict[str, list[dict]] = {}
        self.puts: list[tuple] = []
        self.put_error = None

    async def patch_member(self, community, member_key, body):
        self.patches.append((community, member_key, body.to_dict()))
        return SimpleNamespace(status_code=self.status, content=b'{"detail": "Ada"}')

    async def put_delivery_point(self, community, member_key, point_id, body, *, replaces=None):
        """rec-registry 1.7.0: add, remove `replaces`, relink meters, in one write."""
        from celine.sdk.rec_registry import RecRegistryApiError

        self.puts.append((community, member_key, point_id, dict(body), replaces))
        if self.put_error is not None:
            raise self.put_error
        held = self.points.setdefault(member_key, [])
        if replaces is not None:
            match = [p for p in held if p.strip().lower() == replaces.strip().lower()]
            if not match:
                raise RecRegistryApiError(
                    f"put-delivery-point: Delivery point '{replaces}' not found", status_code=404
                )
            held.remove(match[0])
            for meter in self.meters.get(member_key, []):
                if meter["pod"].strip().lower() == replaces.strip().lower():
                    meter["pod"] = point_id
        if point_id not in held:
            held.append(point_id)
        return SimpleNamespace(delivery_points=[SimpleNamespace(id=p) for p in held])

    # The POD export's two lookups (`supply_points_by_did`).
    async def lookup_members_by_dids(self, dids):
        return [
            SimpleNamespace(
                key=key,
                did=DID,
                community_key=COMMUNITY,
                status="active",
                user_id=USERNAME,
                delivery_points=[SimpleNamespace(id=p, active=True) for p in points],
            )
            for key, points in self.points.items()
            if DID in dids
        ]

    async def lookup_assets_by_user_ids(self, user_ids):
        return []


class FakeIdentityRegistry:
    """`/admin/keycloak/sync` and `/users/resolve`, over one mapping table."""

    def __init__(self):
        self.mappings = {DID: {"email": OLD_EMAIL, "username": USERNAME, "kc": KC_USER}}
        self.syncs: list[dict] = []
        self.sync_status = 200

    def handle(self, req: httpx.Request) -> httpx.Response:
        import json

        url = str(req.url)
        if url.endswith("/admin/keycloak/sync"):
            body = json.loads(req.content)
            self.syncs.append(body)
            if self.sync_status >= 400:
                return httpx.Response(self.sync_status, json={"detail": "no"})
            mapping = self.mappings.setdefault(body["did"], {})
            mapping["email"] = body.get("email")
            mapping["kc"] = body["keycloak_user_id"]
            if body.get("username"):
                mapping["username"] = body["username"]
            return httpx.Response(200, json={"status": "synced", "did": body["did"]})
        if "/users/resolve" in url:
            sent = json.loads(req.content) if req.method == "POST" else req.url.params
            email = sent.get("email")
            for did, mapping in self.mappings.items():
                if mapping.get("email") == email:
                    from test_member_sharing import RESOLVE_WITH_CREDENTIAL

                    return httpx.Response(
                        200, json={**RESOLVE_WITH_CREDENTIAL, "did": did, "subject_id": did}
                    )
            return httpx.Response(404, json={"detail": "no mapping"})
        raise AssertionError(f"unexpected call to {url}")


@pytest.fixture()
def world(monkeypatch, trail):
    prov = FakeProvisioning()
    registry = FakeRegistry()
    idreg = FakeIdentityRegistry()
    login = {
        "keycloak_user": SimpleNamespace(
            status=EnablementStatus.SUCCEEDED, completed_at=datetime.now(UTC), external_ref=KC_USER
        )
    }

    async def _noop():
        return None

    async def _steps(db, submission_id):
        return login

    async def _community(rec_slug):
        return COMMUNITY

    async def _access():
        return SimpleNamespace(base_url="http://ir:30005", headers={"Authorization": "Bearer t"})

    async def _sync_headers(alias):
        return {"Authorization": "Bearer t"}

    def _factory(**kw):
        kw.pop("transport", None)
        return _OriginalAsyncClient(transport=httpx.MockTransport(idreg.handle), **kw)

    monkeypatch.setattr(template_service, "ensure_fresh", _noop)
    monkeypatch.setattr(
        template_service,
        "rec_registry_binding",
        lambda slug: SimpleNamespace(enabled=True, community=COMMUNITY),
    )
    monkeypatch.setattr(
        template_service,
        "dataspace_binding",
        lambda slug: SimpleNamespace(enabled=True, organization="example-rec"),
    )
    monkeypatch.setattr(enablement, "load_steps", _steps)
    monkeypatch.setattr(provisioning, "_get_client", lambda: prov)
    monkeypatch.setattr(provisioning, "participant_community", _community)
    monkeypatch.setattr(provisioning, "keycloak_realm", lambda: "celine")
    monkeypatch.setattr(rec_registry, "_get_client", lambda: registry)
    monkeypatch.setattr(dataspace_identity, "registry_access", _access)
    # The re-sync is the community's act (its collector client); which token it
    # carries is `test_keycloak_sync_and_misalignment.py`'s business.
    monkeypatch.setattr(dataspace_identity, "keycloak_sync_headers", _sync_headers)
    monkeypatch.setattr(dataspace_identity, "_KC_SYNC_MAX_RETRIES", 1)
    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    monkeypatch.setattr(settings, "provisioning_url", "http://provisioning:8010")
    monkeypatch.setattr(settings, "rec_registry_url", "http://registry:8000")
    monkeypatch.setattr(settings, "dataspace_enabled", True)
    return SimpleNamespace(prov=prov, registry=registry, idreg=idreg, login=login)


@pytest.fixture()
def trail(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr(audit_service, "record", lambda db, **kw: rows.append(kw))
    return rows


async def correct(submission, field, value, *, db=None):
    db = db or FakeDb()
    outcome = await revision.record(
        db,
        submission,
        field=field,
        value=value,
        method=RevisionMethod.OFFLINE,
        note="checked with the member",
        actor=OPERATOR,
    )
    await propagation.start(db, outcome)
    return outcome.revision


def states(row) -> dict[str, str]:
    return {s.step: s.status for s in row.steps}


# ── names ────────────────────────────────────────────────────────────────────


async def test_a_name_reaches_the_account_and_the_registry_member(world):
    submission = approved()

    row = await correct(submission, RevisableField.FIRST_NAME, "Augusta")

    assert states(row) == {"account_profile": "done", "registry_name": "done"}
    # Found by (community, member key); only the corrected field is sent, never a
    # username.
    assert world.prov.updates == [
        {"community": COMMUNITY, "key": submission.ref, "first_name": "Augusta"}
    ]
    # Built the way approval builds it.
    assert world.registry.patches == [(COMMUNITY, submission.ref, {"name": "Augusta Lovelace"})]
    assert [s.attempts for s in row.steps] == [1, 1]


async def test_a_name_sends_no_email(world):
    await correct(approved(), RevisableField.LAST_NAME, "King")
    assert world.prov.invitations == []
    assert world.idreg.syncs == []


async def test_a_registry_refusal_fails_its_step_alone(world):
    world.registry.status = 503

    row = await correct(approved(), RevisableField.LAST_NAME, "King")

    assert states(row) == {"account_profile": "done", "registry_name": "failed"}
    failed = row.steps[1]
    assert failed.error_code == "http_503"
    # The registry's body quoted the name; the row does not.
    assert "Ada" not in (failed.reason or "")


# ── email ────────────────────────────────────────────────────────────────────


async def test_an_email_reaches_the_account_then_the_mapping_same_did(world):
    submission = approved()
    world.prov.invitation_code = "has_password"

    row = await correct(submission, RevisableField.EMAIL, NEW_EMAIL)

    assert states(row) == {
        "account_profile": "done",
        "invitation": "skipped",
        "identity_mapping": "done",
    }
    assert world.prov.updates == [
        {"community": COMMUNITY, "key": submission.ref, "email": NEW_EMAIL}
    ]
    assert row.steps[0].outcome == "sent"  # the verification link, to the new address
    # Same DID, same Keycloak user, new address, the username kept.
    assert world.idreg.syncs == [
        {
            "did": DID,
            "keycloak_realm": "celine",
            "keycloak_user_id": KC_USER,
            "email": NEW_EMAIL,
            "username": USERNAME,
        }
    ]
    assert world.idreg.mappings[DID]["email"] == NEW_EMAIL


async def test_a_member_who_never_set_a_password_is_invited_again_at_the_new_address(world):
    world.prov.invitation_code = "sent"

    row = await correct(approved(), RevisableField.EMAIL, NEW_EMAIL)

    assert states(row)["invitation"] == "done"
    # Sent after the account changed, so to the new address only.
    assert world.prov.invitations == [(COMMUNITY, row.submission.ref, "invitation", NEW_EMAIL)]


async def test_email_taken_fails_and_nothing_downstream_moves(world):
    world.prov.update_error = ProvisioningApiError("taken", status_code=409, code="email_taken")

    row = await correct(approved(), RevisableField.EMAIL, NEW_EMAIL)

    assert states(row) == {
        "account_profile": "failed",
        "invitation": "pending",
        "identity_mapping": "pending",
    }
    assert row.steps[0].error_code == "email_taken"
    assert "Another account already uses this email address" in row.steps[0].reason
    assert row.steps[2].reason == "Waits for account_profile."
    # The mapping is not moved ahead of the account, or the member would lose
    # their Data sharing page while still signing in with the old address.
    assert world.idreg.syncs == []
    assert world.prov.invitations == []


async def test_a_disabled_account_fails_with_that_reason(world):
    world.prov.update_error = ProvisioningApiError("off", status_code=409, code="account_disabled")

    row = await correct(approved(), RevisableField.EMAIL, NEW_EMAIL)

    assert row.steps[0].status == "failed"
    assert row.steps[0].error_code == "account_disabled"
    assert "disabled" in row.steps[0].reason


async def test_a_pre_1_4_service_is_named(world):
    world.prov.update_error = ProvisioningApiError("no", status_code=405, code=None)

    row = await correct(approved(), RevisableField.FIRST_NAME, "Augusta")

    assert row.steps[0].error_code == "http_405"
    assert "1.4.0" in row.steps[0].reason


async def test_a_mapping_conflict_is_not_a_retry(world):
    world.idreg.sync_status = 409

    row = await correct(approved(), RevisableField.EMAIL, NEW_EMAIL)

    assert states(row)["identity_mapping"] == "failed"
    assert row.steps[2].error_code == "mapping_conflict"


async def test_a_member_outside_the_dataspace_skips_the_mapping(world):
    submission = approved()
    submission.dataspace_did = None

    row = await correct(submission, RevisableField.EMAIL, NEW_EMAIL)

    assert states(row)["identity_mapping"] == "skipped"
    assert world.idreg.syncs == []


async def test_a_deployment_with_no_provisioning_skips_the_account(world, monkeypatch):
    monkeypatch.setattr(settings, "provisioning_url", "")

    row = await correct(approved(), RevisableField.FIRST_NAME, "Augusta")

    assert states(row) == {"account_profile": "skipped", "registry_name": "done"}


# ── the member still reaches their Data sharing page ─────────────────────────


async def test_after_the_correction_the_member_reaches_their_sharing_page(world, monkeypatch):
    """The page resolves the member by their token's email at the identity
    registry; after the account and the mapping move, the new address resolves to
    the same DID and the old one to nothing."""
    from test_member_sharing import _member

    import celine.onboarding.services.member_sharing as ms

    monkeypatch.setattr(ms.settings, "dataspace_enabled", True)
    monkeypatch.setattr(
        template_service, "recs_for_organization", lambda alias: ["rec-a"] if alias else []
    )
    monkeypatch.setattr(dataspace_identity.settings, "dataspace_user_role", "DataSubject")

    state, _, credential = await ms._resolve(_member("rec-a", email=OLD_EMAIL), provision=False)
    assert state is ms.SharingState.OK and credential.subject_id == DID

    await correct(approved(), RevisableField.EMAIL, NEW_EMAIL)

    state, _, credential = await ms._resolve(_member("rec-a", email=NEW_EMAIL), provision=False)
    assert state is ms.SharingState.OK
    assert credential.subject_id == DID
    state, _, _ = await ms._resolve(_member("rec-a", email=OLD_EMAIL), provision=False)
    assert state is ms.SharingState.NO_IDENTITY


# ── statuses, superseding, retry ─────────────────────────────────────────────


async def test_before_approval_nothing_propagates(world):
    row = await correct(
        make_submission(SubmissionStatus.UNDER_REVIEW), RevisableField.EMAIL, NEW_EMAIL
    )
    assert row.steps == []
    assert world.prov.updates == []


async def test_a_revoked_member_gets_every_step_skipped(world):
    world.login["keycloak_user"] = SimpleNamespace(
        status=EnablementStatus.PENDING, completed_at=datetime.now(UTC), external_ref=None
    )

    row = await correct(approved(), RevisableField.EMAIL, NEW_EMAIL)

    assert set(states(row).values()) == {"skipped"}
    assert all(s.reason == "Not propagated: enablement revoked." for s in row.steps)
    assert world.prov.updates == []


async def test_a_later_correction_supersedes_unfinished_steps(world):
    submission = approved()
    world.prov.update_error = ProvisioningApiError("x", status_code=502, code="provisioning_failed")
    first = await correct(submission, RevisableField.FIRST_NAME, "Augusta")
    assert states(first)["account_profile"] == "failed"

    world.prov.update_error = None
    second = await correct(submission, RevisableField.FIRST_NAME, "Ava")

    assert states(first) == {"account_profile": "skipped", "registry_name": "done"}
    assert first.steps[0].reason == f"Superseded by revision {second.id}."
    assert states(second) == {"account_profile": "done", "registry_name": "done"}


async def test_a_retry_runs_what_failed_and_sends_what_is_in_force(world):
    submission = approved()
    world.prov.update_error = ProvisioningApiError("x", status_code=502, code="send_failed")
    world.prov.invitation_code = "has_password"
    row = await correct(submission, RevisableField.EMAIL, NEW_EMAIL)
    assert states(row)["account_profile"] == "failed"

    world.prov.update_error = None
    await propagation.run(FakeDb(), submission, row)

    assert states(row) == {
        "account_profile": "done",
        "invitation": "skipped",
        "identity_mapping": "done",
    }
    assert row.steps[0].attempts == 2
    assert row.steps[0].error_code is None


async def test_a_mapping_retried_alone_reads_the_username_back(world):
    submission = approved()
    world.idreg.sync_status = 503
    world.prov.invitation_code = "has_password"
    row = await correct(submission, RevisableField.EMAIL, NEW_EMAIL)
    assert states(row)["identity_mapping"] == "failed"

    world.idreg.sync_status = 200
    await propagation.run(FakeDb(), submission, row, only=revision.PropagationStep.IDENTITY_MAPPING)

    assert states(row)["identity_mapping"] == "done"
    # A no-op correction (the account already has the address) gave the username.
    assert world.prov.updates[-1] == {
        "community": COMMUNITY,
        "key": submission.ref,
        "email": NEW_EMAIL,
    }
    assert world.idreg.syncs[-1]["username"] == USERNAME
    # Only one invitation decision, from the first run.
    assert len(world.prov.invitations) == 1


# ── no values anywhere ───────────────────────────────────────────────────────


async def test_rows_and_log_hold_no_values(world, caplog):
    caplog.set_level(logging.DEBUG)
    world.registry.status = 500
    submission = approved()

    rows = [
        await correct(submission, RevisableField.EMAIL, NEW_EMAIL),
        await correct(submission, RevisableField.LAST_NAME, "Byron-King"),
    ]

    written = caplog.text + " ".join(
        f"{s.reason} {s.outcome} {s.error_code}" for r in rows for s in r.steps
    )
    for value in (NEW_EMAIL, OLD_EMAIL, USERNAME, "Byron-King", "Ada"):
        assert value not in written


# ── the admin API ─────────────────────────────────────────────────────────────


SUBMISSION_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
URL = f"/api/admin/rec-a/submissions/{SUBMISSION_ID}/revisions"


@pytest.fixture()
def api(monkeypatch, seed_rec, world):
    seed_rec("rec-a", name="REC A", organization=ORG, steps=["consents", "personal", "review"])
    from celine.onboarding.api.admin import revisions as revisions_api
    from celine.onboarding.models.database import get_db

    state = {"submission": approved()}

    async def _owned(db, submission_id, rec_slug):
        from fastapi import HTTPException

        if submission_id != SUBMISSION_ID:
            raise HTTPException(404, "Submission not found")
        return state["submission"]

    monkeypatch.setattr(revisions_api, "_owned_submission", _owned)
    # `seed_rec` replaced the manifest; keep the bindings the world set.
    app = FastAPI()
    app.add_middleware(AdminAuthMiddleware)
    app.include_router(create_admin_router())

    async def _db():
        yield FakeDb()

    app.dependency_overrides[get_db] = _db
    return TestClient(app), state


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_the_answer_carries_the_steps(api, operator_token):
    client, _ = api
    body = {"field": "first_name", "value": "Augusta", "method": "offline", "note": "bill"}

    res = client.post(URL, json=body, headers=auth(operator_token(ORG, "managers")))

    assert res.status_code == 201, res.text
    assert [(s["step"], s["status"]) for s in res.json()["steps"]] == [
        ("account_profile", "done"),
        ("registry_name", "done"),
    ]


def test_retry_needs_revise_and_a_propagating_revision(api, operator_token, world):
    client, state = api
    managers = auth(operator_token(ORG, "managers"))
    world.registry.status = 503
    created = client.post(
        URL,
        json={"field": "last_name", "value": "King", "method": "offline", "note": "bill"},
        headers=managers,
    ).json()
    assert created["steps"][1]["status"] == "failed"

    retry = f"{URL}/{created['id']}/retry"
    assert (
        client.post(retry, json={}, headers=auth(operator_token(ORG, "editors"))).status_code == 403
    )
    assert client.post(f"{URL}/{uuid.uuid4()}/retry", json={}, headers=managers).status_code == 404

    world.registry.status = 200
    res = client.post(retry, json={"step": "registry_name"}, headers=managers)
    assert res.status_code == 200, res.text
    assert res.json()["steps"][1]["status"] == "done"
    assert res.json()["steps"][1]["attempts"] == 2


def test_a_revision_before_approval_has_nothing_to_retry(api, operator_token):
    client, state = api
    state["submission"] = make_submission(SubmissionStatus.UNDER_REVIEW)
    managers = auth(operator_token(ORG, "managers"))
    created = client.post(
        URL,
        json={"field": "last_name", "value": "King", "method": "offline", "note": "bill"},
        headers=managers,
    ).json()

    res = client.post(f"{URL}/{created['id']}/retry", json={}, headers=managers)
    assert res.status_code == 409


async def test_a_named_retry_releases_the_steps_waiting_for_it(world):
    submission = approved()
    world.prov.update_error = ProvisioningApiError("x", status_code=502, code="send_failed")
    world.prov.invitation_code = "sent"
    row = await correct(submission, RevisableField.EMAIL, NEW_EMAIL)
    assert states(row)["identity_mapping"] == "pending"

    world.prov.update_error = None
    await propagation.run(FakeDb(), submission, row, only=revision.PropagationStep.ACCOUNT_PROFILE)

    assert states(row) == {
        "account_profile": "done",
        "invitation": "done",
        "identity_mapping": "done",
    }


class TestTheMigration:
    def test_upgrade_creates_the_table_the_model_describes(self, monkeypatch):
        from test_sharing_intent import _offline_sql

        from celine.onboarding.models.revision import SubmissionRevisionStep

        sql = _offline_sql(monkeypatch, "0017:0018")

        assert "CREATE TABLE submission_revision_steps" in sql
        for column in SubmissionRevisionStep.__table__.columns:
            assert f"\n    {column.name} " in sql, column.name
        assert (
            "FOREIGN KEY(revision_id) REFERENCES submission_revisions (id) ON DELETE CASCADE" in sql
        )
        assert "CONSTRAINT uq_revision_step UNIQUE (revision_id, step)" in sql

    def test_downgrade_drops_it(self, monkeypatch):
        from test_sharing_intent import _offline_sql

        sql = _offline_sql(monkeypatch, "0018:0017", downgrade=True)
        assert "DROP TABLE submission_revision_steps" in sql
