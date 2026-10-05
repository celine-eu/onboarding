"""A subject id this service mints is a random UUID, and a person keeps the one they have.

ds `D-22c`: whoever generates a subject id must not make it revealing, because it
becomes the `<id>` of the person's DID verbatim and travels in every consent record,
provenance event and credential that names them. So the id is minted with
`uuid.uuid4()` and derived from nothing. What used to select it,
`DATASPACE_SUBJECT_SOURCE`, is gone (the maintainer, 2026-09-21).

A random id is not deterministic, so whatever is not recorded is lost and the next
attempt would mint a second DID for the same person. That is why most of this file is
about *reuse*: an existing registry mapping first, then the id a submission already
recorded, and only then a new one.

The registry here is a faithful fake of the anchor's behaviour as read from
`identity_registry/api/v1/users.py` and `admin.py`: `/users/resolve` answers a mapping,
derives only when asked to (`derive=true`), and otherwise answers 404; issuance creates
a DID and never a mapping; only `/admin/keycloak/sync` writes one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import celine.onboarding.main as app_main
import celine.onboarding.services.dataspace_identity as di
from celine.onboarding.config.settings import Settings

_OriginalAsyncClient = httpx.AsyncClient

PARTICIPANT = "did:web:rec.example"
REALM = "celine"


class FakeRegistry:
    """The anchor, as far as provisioning can see it."""

    def __init__(self, *, sync_status: int = 200) -> None:
        self.mappings: dict[str, str] = {}  # lower-cased email -> DID
        self.dids: set[str] = set()
        self.issued_subject_ids: list[str] = []
        self.resolve_params: list[dict[str, str]] = []
        self.sync_status = sync_status

    def map(self, email: str, did: str) -> None:
        self.mappings[email.lower()] = did
        self.dids.add(did)

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/users/resolve":
            # POST with the identifiers in a JSON body; ds serves no GET.
            if req.method != "POST":
                return httpx.Response(405, json={"detail": "Method Not Allowed"})
            params = json.loads(req.content)
            self.resolve_params.append(params)
            email = (params.get("email") or "").lower()
            did = self.mappings.get(email)
            if did:
                return httpx.Response(
                    200, json={"did": did, "subject_id": did.rsplit(":users:", 1)[1]}
                )
            if params.get("derive") == "true" and email:
                digest = hashlib.sha256(email.encode()).hexdigest()[:24]
                return httpx.Response(200, json={"subject_id": f"email-{digest}"})
            return httpx.Response(404, json={"detail": "No mapping found for this user"})
        if path == "/admin/credentials/data-subject":
            body = json.loads(req.content)
            self.issued_subject_ids.append(body["subject_id"])
            did = f"{body['linked_participant_did']}:users:{body['subject_id']}"
            self.dids.add(did)
            return httpx.Response(
                201,
                json={
                    "subjectDid": did,
                    "credentialId": f"urn:uuid:{uuid.uuid4()}",
                    "generatedAt": "2026-09-21T10:00:00Z",
                },
            )
        if path == "/admin/memberships" or path.startswith("/admin/memberships/"):
            return httpx.Response(201 if req.method == "POST" else 204, json={})
        if path == "/admin/keycloak/sync":
            if self.sync_status >= 400:
                return httpx.Response(self.sync_status, text="sync refused")
            body = json.loads(req.content)
            if body.get("email"):
                self.mappings[body["email"].lower()] = body["did"]
            return httpx.Response(200, json={"status": "synced", "did": body["did"]})
        if path.startswith("/admin/credentials/"):
            return httpx.Response(204)
        return httpx.Response(404)


@pytest.fixture()
def registry(monkeypatch, bind_rec):
    bind_rec("default", organization="rec-example", linked_participant_did=PARTICIPANT)
    monkeypatch.setattr(di.settings, "dataspace_enabled", True)
    monkeypatch.setattr(di.settings, "identity_registry_url", "http://ir:30005")
    monkeypatch.setattr(di.settings, "ds_connector_url", "")

    token = MagicMock()
    token.access_token = "test-token"
    provider = AsyncMock()
    provider.get_token.return_value = token
    monkeypatch.setattr(di, "_token_provider", provider)

    fake = FakeRegistry()
    transport = httpx.MockTransport(fake.handler)

    def factory(**kw):
        kw.pop("transport", None)
        return _OriginalAsyncClient(transport=transport, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return fake


def _submission(email: str = "mario.rossi@example.org", ref: str | None = None) -> MagicMock:
    sub = MagicMock()
    sub.ref = ref or f"{datetime.now(UTC):%Y%m%d}-abcd1234"
    sub.email = email
    sub.rec_slug = "default"
    sub.dataspace_vc_id = None
    sub.dataspace_subject_id = None
    sub.dataspace_did = None
    sub.dataspace_vc_issued_at = None
    sub.verification = None
    return sub


async def _onboard(sub: MagicMock, *, keycloak_user_id: str | None = "kc-user-1") -> None:
    await di.provision_user_identity(
        sub,
        keycloak_user_id=keycloak_user_id,
        keycloak_realm=REALM if keycloak_user_id else None,
        keycloak_username=sub.email,
        provision_shares=False,
    )


def _uuid4(value: str) -> uuid.UUID:
    parsed = uuid.UUID(value)
    assert parsed.version == 4, f"{value!r} is a UUID, but not version 4"
    assert str(parsed) == value, f"{value!r} is not in canonical form"
    return parsed


def _select_retired_source(monkeypatch, value: str) -> None:
    """Select an old subject source the way a deployment would have.

    Once the setting is gone there is nothing to set — pydantic refuses an unknown
    attribute — and that is exactly the state these tests want to be in.
    """
    try:
        monkeypatch.setattr(di.settings, "dataspace_subject_source", value)
    except (AttributeError, ValueError):
        pass


# ── a new person ─────────────────────────────────────────────────────


async def test_a_new_person_gets_a_uuid4_subject_id(registry):
    sub = _submission()

    await _onboard(sub)

    assert len(registry.issued_subject_ids) == 1
    issued = registry.issued_subject_ids[0]
    _uuid4(issued)
    assert sub.dataspace_subject_id == issued
    assert sub.dataspace_did == f"{PARTICIPANT}:users:{issued}"
    # The lookup never asked the registry to invent an id.
    assert all(p.get("derive") != "true" for p in registry.resolve_params)


# ── a person the registry already knows ──────────────────────────────


async def test_a_person_with_a_mapping_keeps_their_subject_id(registry):
    """No second DID. The registry's answer to a mapped person is reused as is,
    whatever its shape — here the `email-<24hex>` the registry's own derivation mints,
    which is what a member provisioned outside onboarding may already hold."""
    existing = f"{PARTICIPANT}:users:email-0123456789abcdef01234567"
    registry.map("mario.rossi@example.org", existing)
    sub = _submission()

    await _onboard(sub)

    assert registry.issued_subject_ids == ["email-0123456789abcdef01234567"]
    assert sub.dataspace_did == existing
    assert registry.dids == {existing}
    assert all(p.get("derive") != "true" for p in registry.resolve_params)


async def test_a_second_onboarding_of_the_same_person_is_idempotent(registry):
    first, second = _submission(), _submission(ref="20260922-00ff00ff")

    await _onboard(first)
    await _onboard(second)

    assert len(set(registry.issued_subject_ids)) == 1
    _uuid4(registry.issued_subject_ids[0])
    assert first.dataspace_did == second.dataspace_did
    assert len(registry.dids) == 1


async def test_a_retry_after_a_failed_step_reuses_the_id_it_recorded(registry):
    """Issuance creates the DID; a failure after it must not cost the id.

    The Keycloak sync is what writes the mapping, and here it fails every retry, so
    the registry holds a DID and no mapping. Enablement commits the failed step with
    the submission, so the id recorded before issuance survives to the retry.
    """
    sub = _submission()
    registry.sync_status = 500

    with pytest.raises(ValueError, match="Keycloak sync failed"):
        await _onboard(sub)

    recorded = sub.dataspace_subject_id
    assert recorded, "the minted id was not recorded before issuance"
    _uuid4(recorded)

    registry.sync_status = 200
    await _onboard(sub)

    assert registry.issued_subject_ids == [recorded, recorded]
    assert len(registry.dids) == 1


async def test_a_step_retried_without_a_login_reuses_the_recorded_id(registry):
    """A retry of the identity step alone has no step-1 row, so no Keycloak ids and
    no sync — nothing records a mapping. The submission's own record is then the
    only thing standing between this person and a second DID."""
    sub = _submission()
    await _onboard(sub, keycloak_user_id=None)
    first = sub.dataspace_subject_id

    # Revoked and re-approved: the credential columns clear, the subject id stays.
    sub.dataspace_vc_id = None
    await _onboard(sub, keycloak_user_id=None)

    _uuid4(first)
    assert registry.issued_subject_ids == [first, first]


# ── the id reveals nothing ───────────────────────────────────────────


@pytest.mark.parametrize("retired_source", ["submission_ref", "email_hash", "ref", "email"])
async def test_the_id_never_contains_the_email_its_local_part_or_a_date(
    registry, monkeypatch, retired_source
):
    """Whatever an old deployment still has set. `submission_ref` put the onboarding
    date in the DID; a random id carries nothing at all."""
    _select_retired_source(monkeypatch, retired_source)
    sub = _submission(email="Mario.Rossi@Example.org")

    await _onboard(sub)

    issued = registry.issued_subject_ids[0].lower()
    today = datetime.now(UTC)
    for revealing in (
        "mario.rossi@example.org",
        "mario.rossi",
        "mario",
        "rossi",
        sub.ref.lower(),
        f"{today:%Y%m%d}",
        f"{today:%Y-%m-%d}",
    ):
        assert revealing not in issued, f"{issued!r} contains {revealing!r}"
    _uuid4(registry.issued_subject_ids[0])


async def test_the_wizard_mints_a_uuid_for_a_member_with_no_mapping(registry):
    """The preregistration door resolves the same way: no mapping, a random id."""
    access = await di.registry_access()

    resolved, credential = await di.resolve_subject_and_credential(
        access, email="mario.rossi@example.org"
    )

    assert credential is None
    assert resolved.did is None
    _uuid4(resolved.subject_id)
    assert all(p.get("derive") != "true" for p in registry.resolve_params)


# ── the setting is gone ──────────────────────────────────────────────


def test_the_subject_source_setting_is_gone(tmp_path):
    """A leftover value is accepted and read by nothing.

    Accepted, because pydantic-settings refuses an unknown key in a `.env` file: a
    field deleted outright would stop a deployment that still carries the line from
    booting, with a validation error that says nothing about why.
    """
    env = tmp_path / ".env"
    env.write_text("DATASPACE_SUBJECT_SOURCE=submission_ref\n")

    loaded = Settings(_env_file=str(env))

    assert not hasattr(loaded, "dataspace_subject_source")
    assert "dataspace_subject_source" not in Settings.model_fields


def test_a_leftover_subject_source_is_reported_at_boot(monkeypatch, caplog):
    monkeypatch.setattr(app_main.settings, "removed_dataspace_subject_source", "submission_ref")

    with caplog.at_level(logging.WARNING):
        app_main._warn_removed_subject_source()

    assert any("DATASPACE_SUBJECT_SOURCE" in r.message for r in caplog.records)


def test_no_leftover_no_warning(monkeypatch, caplog):
    monkeypatch.setattr(app_main.settings, "removed_dataspace_subject_source", "")

    with caplog.at_level(logging.WARNING):
        app_main._warn_removed_subject_source()

    assert not any("DATASPACE_SUBJECT_SOURCE" in r.message for r in caplog.records)
