"""Releasing a member from their REC, keyed on the registry's pair.

What is pinned: the four steps run in the revocation's order on their end state,
a step waits for the one it depends on, a failure is a step result and never an
exception, calling again is the retry, a member imported into the registry (no
submission) is released like one onboarded here, and only this community's
credential is revoked. Every dependency is a fake recording what it was asked.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import jwt as pyjwt
import pytest

from celine.onboarding.models.enablement import (
    EnablementStatus,
    SubmissionEnablementStep,
)
from celine.onboarding.services import (
    dataspace_identity,
    enablement,
    member_release,
    provisioning,
    rec_registry,
    template_service,
)
from celine.onboarding.services.provisioning import LoginRelease

COMMUNITY = "example-rec"
REC = "example-rec-slug"
KEY = "ex-00001"
DID = "did:web:rec.example.org:users:ex-1"
COMMUNITY_DID = "did:web:rec.example.org"


def a_credential(*, sub=DID, linked=COMMUNITY_DID, jti="urn:uuid:cred-1", role="data_subject"):
    """A VC-JWT as the identity registry signs one: `jti` is the credential id."""
    token = pyjwt.encode(
        {
            "sub": sub,
            "jti": jti,
            "vc": {"id": jti, "credentialSubject": {"id": sub, "linkedParticipant": linked}},
        },
        "not-the-registry-key-but-unverified-here",
        algorithm="HS256",
    )
    return {"role": role, "vc_jws": token}


class FakeDb:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class World:
    """Every dependency of the release, recording calls in order."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.member = SimpleNamespace(user_id="ex-person@example.org", did=DID, status="active")
        self.submission = None
        self.rows: dict[str, SubmissionEnablementStep] = {}
        self.resolved = {"did": DID, "credentials": [a_credential()]}
        self.fail: dict[str, int] = {}  # step -> how many more times it fails
        self.login_code = "released"
        self.registry_error: Exception | None = None
        #: credential ids the identity registry now holds revoked
        self.revoked: set[str] = set()

    def _maybe_fail(self, step: str) -> None:
        if self.fail.get(step, 0) > 0:
            self.fail[step] -= 1
            raise ValueError(f"{step} refused (503)")


@pytest.fixture()
def world(monkeypatch) -> World:
    w = World()
    s = member_release.settings
    monkeypatch.setattr(s, "rec_registry_url", "http://registry.test")
    monkeypatch.setattr(s, "ds_connector_url", "http://connector.test")
    monkeypatch.setattr(s, "identity_registry_url", "http://ir.test")
    monkeypatch.setattr(s, "provisioning_url", "http://provisioning.test")
    monkeypatch.setattr(s, "dataspace_user_role", "data_subject")

    async def registry_member(community, key):
        w.calls.append(f"registry_member:{community}/{key}")
        if w.registry_error:
            raise w.registry_error
        return w.member if key == KEY else None

    async def submission_for(db, rec_slug, key):
        return w.submission

    async def withdraw(subject, *, raise_on_error=False, report=None, **_):
        w.calls.append(f"withdraw:{subject.dataspace_did}")
        w._maybe_fail("share")
        if report is not None:
            report.append("offer-1 withdrawn at the community's connector")
        subject.share_provisioned = False
        return True

    async def registry_access():
        return SimpleNamespace(base_url="http://ir.test", headers={})

    async def resolve(access, params):
        w.calls.append(f"resolve:{params}")
        active = [
            c
            for c in w.resolved["credentials"]
            if pyjwt.decode(c["vc_jws"], options={"verify_signature": False})["jti"]
            not in w.revoked
        ]
        return {**w.resolved, "credentials": active}

    async def revoke_credential(credential_id, binding):
        w.calls.append(f"revoke_credential:{credential_id}")
        w._maybe_fail("credential")
        w.revoked.add(credential_id)

    async def revoke_identity(subject):
        w.calls.append(f"revoke_identity:{subject.dataspace_did}:{subject.dataspace_vc_id}")
        w._maybe_fail("identity")
        cred = subject.dataspace_vc_id
        w.revoked.add(cred)
        subject.dataspace_vc_id = None
        subject.dataspace_did = None
        return f"revoked credential {cred}"

    async def collector_headers(org, scope):
        return {}

    async def delete_membership(base_url, headers, did, org, *, subject_ref=None):
        w.calls.append(f"delete_membership:{did}:{org}")

    async def release_login(community, key):
        w.calls.append(f"release_login:{community}/{key}")
        w._maybe_fail("login")
        return LoginRelease(w.login_code, f"login {w.login_code}")

    async def deactivate(community, key):
        w.calls.append(f"deactivate:{community}/{key}")
        w._maybe_fail("registry")
        return f"deactivated registry member {key}"

    async def ensure_fresh():
        pass

    async def load_steps(db, submission_id):
        return w.rows

    monkeypatch.setattr(rec_registry, "registry_member", registry_member)
    monkeypatch.setattr(rec_registry, "deactivate_registry_member", deactivate)
    monkeypatch.setattr(member_release, "_submission_for", submission_for)
    monkeypatch.setattr(dataspace_identity, "withdraw_user_shares", withdraw)
    monkeypatch.setattr(dataspace_identity, "registry_access", registry_access)
    monkeypatch.setattr(dataspace_identity, "_resolve_raw_with_params", resolve)
    monkeypatch.setattr(dataspace_identity, "revoke_user_identity", revoke_identity)
    monkeypatch.setattr(dataspace_identity, "_collector_headers", collector_headers)
    monkeypatch.setattr(dataspace_identity, "_delete_membership", delete_membership)
    monkeypatch.setattr(provisioning, "release_login", release_login)
    monkeypatch.setattr(member_release, "revoke_credential", revoke_credential)
    monkeypatch.setattr(template_service, "ensure_fresh", ensure_fresh)
    monkeypatch.setattr(
        template_service,
        "dataspace_binding",
        lambda slug: SimpleNamespace(
            organization=COMMUNITY, linked_participant_did=COMMUNITY_DID, enabled=True
        ),
    )
    monkeypatch.setattr(enablement, "load_steps", load_steps)
    return w


async def release(db=None):
    return await member_release.release(
        db or FakeDb(), community=COMMUNITY, rec_slug=REC, member_key=KEY
    )


def by_step(result) -> dict[str, tuple[str, str]]:
    return {s.step: (s.status, s.code) for s in result.steps}


# ---------------------------------------------------------------------------
# A member imported into the registry: no submission
# ---------------------------------------------------------------------------


async def test_an_imported_member_is_released_in_the_revocation_order(world):
    result = await release()

    assert result.source == "registry"
    assert result.state == "released"
    assert [s.step for s in result.steps] == [
        "dataspace_share",
        "dataspace_identity",
        "keycloak_user",
        "rec_registry_member",
    ]
    assert by_step(result) == {
        "dataspace_share": ("done", "withdrawn"),
        "dataspace_identity": ("done", "revoked"),
        "keycloak_user": ("done", "released"),
        "rec_registry_member": ("done", "deactivated"),
    }
    acts = [c for c in world.calls if not c.startswith(("registry_member", "resolve"))]
    # Resolved before (to find the credentials) and after (to prove none remains).
    assert acts == [
        f"withdraw:{DID}",
        f"revoke_identity:{DID}:urn:uuid:cred-1",
        f"release_login:{COMMUNITY}/{KEY}",
        f"deactivate:{COMMUNITY}/{KEY}",
    ]


async def test_the_credential_is_found_by_the_members_login(world):
    await release()
    assert "resolve:{'username': 'ex-person@example.org'}" in world.calls


async def test_another_communitys_credential_is_not_revoked_and_is_reported(world):
    """The person may hold a credential another organisation linked; only this
    community's is its to revoke. Its membership still goes, and the step says
    that one remains: ds moves the login to the next REC's DID only once the old
    DID holds no active credential (ADR-0028)."""
    world.resolved = {"did": DID, "credentials": [a_credential(linked="did:web:other.example.org")]}

    result = await release()

    assert by_step(result)["dataspace_identity"] == ("done", "held_elsewhere")
    assert result.state == "released"
    assert not any(c.startswith("revoke_identity") for c in world.calls)
    assert f"delete_membership:{DID}:{COMMUNITY}" in world.calls


async def test_a_login_that_now_maps_to_another_identity_is_left_alone(world):
    """The person joined another community since: the login resolves to a new DID,
    and nothing of that identity is touched."""
    world.resolved = {
        "did": "did:web:other.example.org:users:x",
        "credentials": [a_credential(sub="did:web:other.example.org:users:x")],
    }

    result = await release()

    assert by_step(result)["dataspace_identity"] == ("done", "no_credential")
    assert not any(c.startswith("revoke_identity") for c in world.calls)


async def test_a_member_with_no_dataspace_identity_skips_both_dataspace_steps(world):
    world.member.did = None

    result = await release()

    assert by_step(result)["dataspace_share"] == ("skipped", "no_dataspace_identity")
    assert by_step(result)["dataspace_identity"] == ("skipped", "no_dataspace_identity")
    assert result.state == "released"
    assert f"release_login:{COMMUNITY}/{KEY}" in world.calls


# ---------------------------------------------------------------------------
# Failures are step results, and calling again is the retry
# ---------------------------------------------------------------------------


async def test_a_failed_withdrawal_blocks_the_identity_and_nothing_else(world):
    world.fail["share"] = 1

    result = await release()

    assert result.state == "partial"
    assert by_step(result) == {
        "dataspace_share": ("failed", "withdrawal_failed"),
        "dataspace_identity": ("blocked", "waits_for_dataspace_share"),
        "keycloak_user": ("done", "released"),
        "rec_registry_member": ("done", "deactivated"),
    }
    assert not any(c.startswith("revoke_identity") for c in world.calls)

    # Again: the withdrawal now lands, and the identity with it.
    world.login_code = "already_released"
    again = await release()
    assert again.state == "released"
    assert by_step(again)["dataspace_identity"] == ("done", "revoked")
    assert by_step(again)["keycloak_user"] == ("done", "already_released")


async def test_the_member_waits_for_a_login_release_that_failed(world):
    """The login is released through the registry; a member deactivated while
    that failed would leave a login nobody here releases."""
    world.fail["login"] = 1

    result = await release()

    assert by_step(result)["keycloak_user"] == ("failed", "release_failed")
    assert by_step(result)["rec_registry_member"] == ("blocked", "waits_for_keycloak_user")
    assert not any(c.startswith("deactivate") for c in world.calls)


async def test_no_account_is_skipped_not_failed(world):
    world.login_code = "no_account"
    result = await release()
    assert by_step(result)["keycloak_user"] == ("skipped", "no_account")
    assert result.state == "released"


async def test_without_a_provisioning_service_the_login_step_is_skipped(world, monkeypatch):
    monkeypatch.setattr(member_release.settings, "provisioning_url", "")
    result = await release()
    assert by_step(result)["keycloak_user"] == ("skipped", "no_provisioning")
    assert by_step(result)["rec_registry_member"] == ("done", "deactivated")


async def test_without_a_connector_the_withdrawal_is_skipped(world, monkeypatch):
    monkeypatch.setattr(member_release.settings, "ds_connector_url", "")
    result = await release()
    assert by_step(result)["dataspace_share"] == ("skipped", "no_connector")


# ---------------------------------------------------------------------------
# Refused before anything changes
# ---------------------------------------------------------------------------


async def test_a_key_the_registry_does_not_hold_changes_nothing(world):
    with pytest.raises(member_release.MemberNotFoundError):
        await member_release.release(
            FakeDb(), community=COMMUNITY, rec_slug=REC, member_key="nobody"
        )
    assert world.calls == [f"registry_member:{COMMUNITY}/nobody"]


async def test_a_registry_that_cannot_be_read_changes_nothing(world):
    world.registry_error = rec_registry.RegistryUnavailableError("503")
    with pytest.raises(rec_registry.RegistryUnavailableError):
        await release()
    assert world.calls == [f"registry_member:{COMMUNITY}/{KEY}"]


# ---------------------------------------------------------------------------
# A member with a submission
# ---------------------------------------------------------------------------


def _row(step, status=EnablementStatus.SUCCEEDED, ref="x"):
    return SubmissionEnablementStep(
        submission_id=uuid.uuid4(), step=step, status=status, attempts=1, external_ref=ref
    )


async def test_a_submission_is_the_source_and_its_rows_tell_the_same_story(world):
    world.submission = SimpleNamespace(
        id=uuid.uuid4(),
        ref=KEY,
        rec_slug=REC,
        dataspace_did=DID,
        dataspace_vc_id="urn:uuid:from-the-submission",
        dataspace_vc_issued_at=None,
        dataspace_subject_id="old-subject-id",
        share_provisioned=True,
    )
    world.rows = {step.value: _row(step) for step in member_release.STEPS}
    world.fail["registry"] = 1
    db = FakeDb()

    result = await release(db)

    assert result.source == "submission"
    # The submission's credential id, not one looked up.
    assert f"revoke_identity:{DID}:urn:uuid:from-the-submission" in world.calls
    assert world.submission.dataspace_did is None  # cleared by the identity step
    # The next approval mints a new identifier, never this one.
    assert world.submission.dataspace_subject_id is None
    rows = world.rows
    for step in ("dataspace_share", "dataspace_identity", "keycloak_user"):
        assert rows[step].status == EnablementStatus.PENDING
        assert rows[step].completed_at is not None
        assert rows[step].external_ref is None
    assert rows["rec_registry_member"].status == EnablementStatus.FAILED
    assert rows["rec_registry_member"].last_error.startswith(enablement.REVOKE_FAILED)
    assert enablement.revoked(rows)
    assert db.commits == 1


async def test_a_submission_with_no_identity_falls_back_to_the_registrys(world):
    """A member who got their dataspace identity on their own sharing page, after
    approval, holds it on the registry row and not on the submission."""
    world.submission = SimpleNamespace(
        id=uuid.uuid4(), ref=KEY, rec_slug=REC, dataspace_did=None, dataspace_vc_id=None
    )

    result = await release()

    assert result.source == "submission"
    assert f"revoke_identity:{DID}:urn:uuid:cred-1" in world.calls


# ---------------------------------------------------------------------------
# Every credential this community linked, whatever its role (ds ADR-0028)
# ---------------------------------------------------------------------------


async def test_every_credential_this_community_linked_is_revoked_whatever_its_role(world):
    world.resolved = {
        "did": DID,
        "credentials": [
            a_credential(jti="urn:uuid:subject"),
            a_credential(jti="urn:uuid:consumer", role="consumer_user"),
        ],
    }

    result = await release()

    assert by_step(result)["dataspace_identity"] == ("done", "revoked")
    assert f"revoke_identity:{DID}:urn:uuid:subject" in world.calls
    assert "revoke_credential:urn:uuid:consumer" in world.calls
    assert world.revoked == {"urn:uuid:subject", "urn:uuid:consumer"}


async def test_a_submission_credential_and_one_the_member_got_later_are_both_revoked(world):
    """The submission recorded one credential; the member's sharing page may have
    issued another since. Both are this community's."""
    world.submission = SimpleNamespace(
        id=uuid.uuid4(),
        ref=KEY,
        rec_slug=REC,
        email="ex-person@example.org",
        dataspace_did=DID,
        dataspace_vc_id="urn:uuid:recorded",
        dataspace_vc_issued_at=None,
        dataspace_subject_id="old",
        share_provisioned=False,
    )
    world.resolved = {
        "did": DID,
        "credentials": [a_credential(jti="urn:uuid:recorded"), a_credential(jti="urn:uuid:later")],
    }

    result = await release()

    assert by_step(result)["dataspace_identity"] == ("done", "revoked")
    assert world.revoked == {"urn:uuid:recorded", "urn:uuid:later"}
    assert [c for c in world.calls if c.startswith("revoke_credential")] == [
        "revoke_credential:urn:uuid:later"
    ]


async def test_a_credential_still_active_after_revoking_is_reported_and_retried(world, monkeypatch):
    world.resolved = {"did": DID, "credentials": [a_credential(jti="urn:uuid:stubborn")]}

    async def revoke_identity_that_does_not_land(subject):
        world.calls.append("revoke_identity")
        subject.dataspace_vc_id = None
        return "revoked"

    monkeypatch.setattr(
        dataspace_identity, "revoke_user_identity", revoke_identity_that_does_not_land
    )

    result = await release()

    assert by_step(result)["dataspace_identity"] == ("failed", "credential_remains")
    assert result.state == "partial"
