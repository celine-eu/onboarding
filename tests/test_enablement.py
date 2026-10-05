"""What approval does, and what happens when part of it does not.

The pipeline was previously three fire-and-forget calls: a failure was a 422 whose
only remedy was pressing Approve again and re-running everything. These tests pin
the behaviour that replaced it — per-step state, a fail-closed boundary, and a
retry that finishes the job rather than restarting it.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from celine.onboarding.models.enablement import (
    EnablementStatus,
    EnablementStep,
    SubmissionEnablementStep,
)
from celine.onboarding.models.submission import SubmissionStatus
from celine.onboarding.models.verification import VerificationMethod
from celine.onboarding.services import (
    dataspace_identity,
    document_service,
    enablement,
    provisioning,
    rec_registry,
)
from celine.onboarding.services.enablement import EnablementError
from celine.onboarding.services.errors import ConfigurationError
from celine.onboarding.services.provisioning import ParticipantProvisionResult


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class FakeDb:
    """Enough AsyncSession for the enablement runner, single-submission scope."""

    def __init__(self) -> None:
        self.rows: list[SubmissionEnablementStep] = []
        self.commits = 0

    def add(self, obj) -> None:
        self.rows.append(obj)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj) -> None:
        pass

    async def execute(self, statement):
        return FakeResult(self.rows)


#: What a recorded offline verification looks like to the code that reads it.
OFFLINE = SimpleNamespace(method="offline", verification_method=VerificationMethod.OFFLINE)


class FakeSubmission:
    def __init__(self, **kwargs):
        self.id = uuid.uuid4()
        self.ref = "20260730-test"
        self.rec_slug = "rec-a"
        self.email = "member@example.org"
        self.data_sharing_consent = True
        self.dataspace_vc_id = None
        self.dataspace_did = None
        self.share_provisioned = False
        # Approved before verifications were recorded, unless a test says otherwise.
        self.verification = None
        self.__dict__.update(kwargs)


@pytest.fixture()
def db() -> FakeDb:
    return FakeDb()


@pytest.fixture()
def submission() -> FakeSubmission:
    return FakeSubmission()


@pytest.fixture(autouse=True)
def discarded(monkeypatch) -> list[str]:
    """The submissions whose uploaded documents were discarded, in order."""
    refs: list[str] = []

    async def _discard(db, sub):
        refs.append(sub.ref)
        return 0

    monkeypatch.setattr(document_service, "discard_documents", _discard)
    return refs


@pytest.fixture()
def happy_path(monkeypatch):
    """Every step succeeds, and records what the runner should carry forward."""
    calls: list[str] = []

    async def _kc(sub):
        calls.append("keycloak_user")
        # What the service answers an upsert that asks for no invitation.
        return ParticipantProvisionResult(
            user_id="kc-123", username=sub.email, created=True, invitation="not_requested"
        )

    async def _invite(sub):
        calls.append("invitation")
        return "sent"

    async def _registry(sub, *, keycloak_username=None):
        calls.append(f"rec_registry_member(user={keycloak_username})")
        return "member-key-1"

    async def _identity(sub, **kwargs):
        calls.append(
            f"dataspace_identity(kc={kwargs.get('keycloak_user_id')}, "
            f"username={kwargs.get('keycloak_username')})"
        )
        sub.dataspace_vc_id = "cred-1"
        sub.dataspace_did = "did:web:member"

    async def _shares(sub, **kwargs):
        calls.append("dataspace_share")
        sub.share_provisioned = True

    async def _set_did(sub, *, member_key, did):
        calls.append(f"set_member_did(member={member_key}, did={did})")
        return f"registry member {member_key} holds the dataspace DID"

    # Step 1 checks both gates before it calls, so all three are stubbed: a
    # community the REC declares, and a provisioning service to call.
    async def _community(rec_slug):
        return "rec-a-community"

    monkeypatch.setattr(provisioning, "provisioning_enabled", lambda: True)
    monkeypatch.setattr(provisioning, "participant_community", _community)
    monkeypatch.setattr(provisioning, "provision_participant", _kc)
    monkeypatch.setattr(provisioning, "invite_participant", _invite)
    monkeypatch.setattr(rec_registry, "register_member", _registry)
    monkeypatch.setattr(rec_registry, "set_member_did", _set_did)
    monkeypatch.setattr(dataspace_identity, "provision_user_identity", _identity)
    monkeypatch.setattr(dataspace_identity, "provision_user_shares", _shares)
    # The step bodies import `settings` at call time, so patching the singleton's
    # attributes is enough.
    from celine.onboarding.config.settings import settings

    monkeypatch.setattr(settings, "ds_connector_url", "http://connector.test")
    monkeypatch.setattr(settings, "dataspace_keycloak_realm", "celine")
    return calls


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


class TestTheLoginStepReportsTheInvitation:
    """Step 1 records the provisioning service's invitation code and a sentence.

    The code comes from the invitation sent once approval can no longer fail,
    not from the upsert, which asks for none (celine-eu/onboarding#8). Every
    outcome is `succeeded`: an invitation that did not go out is not a failed
    login, and failing closed would block approval over an email.
    """

    @pytest.mark.parametrize(
        ("created", "invitation", "detail"),
        [
            (True, "sent", "created, invitation sent"),
            (False, "sent", "already existed, invitation sent"),
            (False, "has_password", "already existed, has a password"),
            (True, "not_on_dev_list", "created, invitation not sent (provisioning email mode)"),
            (
                False,
                "account_disabled",
                "already existed, invitation not sent: account is disabled",
            ),
            (
                True,
                "not_requested",
                "created, invitation not sent yet: it goes out when approval completes",
            ),
            (
                False,
                "cooldown",
                "already existed, invitation not sent: this account was emailed moments ago",
            ),
            (True, "send_failed", "created, invitation not sent: the email could not be sent"),
            (
                False,
                "no_email",
                "already existed, invitation not sent: the account has no email address",
            ),
            # A code the service adds later is shown, not dropped.
            (True, "rate_limited", "created, invitation: rate_limited"),
            # No code at all: the sentence the step wrote before invitations.
            (True, None, "created"),
            (False, None, "already existed"),
        ],
    )
    def test_the_detail_sentence(self, created, invitation, detail):
        assert enablement.login_detail(created, invitation) == detail

    @pytest.fixture()
    def invitation(self, monkeypatch, happy_path):
        outcome = {"code": "account_disabled", "created": False}

        async def _kc(sub):
            return ParticipantProvisionResult(
                user_id="kc-123",
                username=sub.email,
                created=outcome["created"],
                invitation="not_requested",
            )

        async def _invite(sub):
            return outcome["code"]

        monkeypatch.setattr(provisioning, "provision_participant", _kc)
        monkeypatch.setattr(provisioning, "invite_participant", _invite)
        return outcome

    @pytest.mark.parametrize(
        "code",
        [
            "sent",
            "has_password",
            "not_on_dev_list",
            "account_disabled",
            "cooldown",
            "send_failed",
            "no_email",
        ],
    )
    @pytest.mark.parametrize("created", [True, False])
    async def test_every_code_is_a_success_and_is_recorded(
        self, db, submission, invitation, code, created
    ):
        invitation["code"] = code
        invitation["created"] = created
        rows = await enablement.enable(db, submission)
        row = rows[EnablementStep.KEYCLOAK_USER]
        assert row.status == EnablementStatus.SUCCEEDED
        assert row.invitation == code
        # The account half is the upsert's, kept when the invitation's is added.
        assert row.detail == enablement.login_detail(created, code)

    async def test_only_the_login_step_carries_a_code(self, db, submission, invitation):
        rows = await enablement.enable(db, submission)
        assert {step: row.invitation for step, row in rows.items()} == {
            EnablementStep.KEYCLOAK_USER: "account_disabled",
            EnablementStep.REC_REGISTRY_MEMBER: None,
            EnablementStep.DATASPACE_IDENTITY: None,
            EnablementStep.DATASPACE_SHARE: None,
        }

    def test_invited_means_sent(self):
        result = ParticipantProvisionResult(user_id="u", username="n", created=True)
        assert result.invited is False
        for code in (
            "has_password",
            "not_on_dev_list",
            "account_disabled",
            "not_requested",
            "cooldown",
            "send_failed",
            "no_email",
        ):
            assert ParticipantProvisionResult("u", "n", True, invitation=code).invited is False
        assert ParticipantProvisionResult("u", "n", True, invitation="sent").invited is True


class TestEnable:
    async def test_runs_every_step_in_order(self, db, submission, happy_path):
        rows = await enablement.enable(db, submission)

        assert [c.split("(")[0] for c in happy_path] == [
            "keycloak_user",
            "rec_registry_member",
            "dataspace_identity",
            # Not a fifth step. Step 3 writes the DID it just minted onto the
            # member step 2 created, and this list is calls rather than steps.
            "set_member_did",
            "dataspace_share",
            # Not a step either: step 1's invitation, held back until nothing
            # left can fail the approval (celine-eu/onboarding#8).
            "invitation",
        ]
        assert all(r.status == EnablementStatus.SUCCEEDED for r in rows.values())
        assert enablement.state_of(rows) == "complete"

    async def test_records_external_references(self, db, submission, happy_path):
        rows = await enablement.enable(db, submission)
        assert rows[EnablementStep.KEYCLOAK_USER].external_ref == "kc-123"
        assert rows[EnablementStep.REC_REGISTRY_MEMBER].external_ref == "member-key-1"
        assert rows[EnablementStep.DATASPACE_IDENTITY].external_ref == "cred-1"

    async def test_keycloak_user_id_reaches_later_steps(self, db, submission, happy_path):
        """The dataspace sync maps a DID onto the Keycloak user, so the id has to flow."""
        await enablement.enable(db, submission)
        assert any(c.startswith("dataspace_identity(kc=kc-123,") for c in happy_path)

    async def test_the_keycloak_username_reaches_the_registry(self, db, submission, happy_path):
        """Not the user id: the registry resolves a self-service caller by
        `preferred_username`, so a member row keyed on the UUID belongs to
        somebody who can never see it."""
        await enablement.enable(db, submission)
        assert "rec_registry_member(user=member@example.org)" in happy_path

    async def test_the_username_is_the_one_provisioning_reported(
        self, db, submission, happy_path, monkeypatch
    ):
        """An account provisioning found rather than created may log in under a
        name that is not their email, and it is that value the registry needs —
        not the email we asked by."""

        async def _kc(sub):
            happy_path.append("keycloak_user")
            return ParticipantProvisionResult(user_id="kc-123", username="ex-00001", created=False)

        monkeypatch.setattr(provisioning, "provision_participant", _kc)

        await enablement.enable(db, submission)

        assert "rec_registry_member(user=ex-00001)" in happy_path

    async def test_counts_attempts(self, db, submission, happy_path):
        rows = await enablement.enable(db, submission)
        assert all(r.attempts == 1 for r in rows.values())

    async def test_rows_are_created_once(self, db, submission, happy_path):
        await enablement.enable(db, submission)
        await enablement.enable(db, submission)
        assert len(db.rows) == len(enablement.PIPELINE)


# ---------------------------------------------------------------------------
# The dataspace DID reaches the registry member
# ---------------------------------------------------------------------------


class TestTheUsernameReachesTheIdentityRegistry:
    """One person, named the same way by every system that has to agree.

    Step 2 writes the Keycloak username into `Member.user_id`; step 3 must send
    the *same* value to the identity registry, because the connector reads it
    back to name a consenting subject to the data plane and `dataset-api`
    resolves that against `Member.user_id`. Taking it from one provisioning
    result is what stops the two ends drifting apart.
    """

    async def test_the_username_is_passed_to_the_identity_step(self, db, submission, happy_path):
        await enablement.enable(db, submission)

        assert any("username=member@example.org" in c for c in happy_path)

    async def test_it_is_the_same_value_the_member_row_got(self, db, submission, happy_path):
        """Not the email re-derived a second time: one read, two destinations.

        A user this service adopted has a username that is not their email, and
        deriving it separately in each place is how the registry member and the
        dataspace mapping come to disagree about who somebody is.
        """
        await enablement.enable(db, submission)

        member = next(c for c in happy_path if c.startswith("rec_registry_member"))
        identity = next(c for c in happy_path if c.startswith("dataspace_identity"))
        written = member.split("user=")[1].rstrip(")")

        assert f"username={written}" in identity


class TestTheDidReachesTheRegistry:
    """Step 3 mints the DID; the member row is where it has to land.

    The connector answers *who consents* in DIDs and the registry knows *what
    they hold*. Nothing joins the two unless this write happens, and a member
    without a DID is silently absent from every consent-driven export — the
    failure reads as an empty answer rather than as an error.
    """

    async def test_the_minted_did_is_written_to_the_member(self, db, submission, happy_path):
        await enablement.enable(db, submission)

        assert "set_member_did(member=member-key-1, did=did:web:member)" in happy_path

    async def test_it_runs_after_the_identity_that_mints_it(self, db, submission, happy_path):
        """Not a field on the create at step 2: the DID does not exist then."""
        await enablement.enable(db, submission)

        names = [c.split("(")[0] for c in happy_path]
        assert names.index("dataspace_identity") < names.index("set_member_did")

    async def test_the_step_says_what_it_did(self, db, submission, happy_path):
        rows = await enablement.enable(db, submission)

        assert "holds the dataspace DID" in rows[EnablementStep.DATASPACE_IDENTITY].detail

    async def test_no_member_means_nothing_to_write_to(
        self, db, submission, monkeypatch, happy_path
    ):
        """A community with no registry binding skips step 2, so there is no
        member row to carry a DID. That is a supported configuration, not a
        failure."""

        async def _registry(sub, *, keycloak_username=None):
            happy_path.append("rec_registry_member(skipped)")
            return None

        monkeypatch.setattr(rec_registry, "register_member", _registry)

        rows = await enablement.enable(db, submission)

        assert not [c for c in happy_path if c.startswith("set_member_did")]
        assert rows[EnablementStep.DATASPACE_IDENTITY].status == EnablementStatus.SUCCEEDED

    async def test_no_identity_means_nothing_to_write(
        self, db, submission, monkeypatch, happy_path
    ):
        """A community outside the dataspace mints no DID, so step 3 skips and
        the registry is not patched with nothing."""

        async def _identity(sub, **kwargs):
            happy_path.append("dataspace_identity(skipped)")

        monkeypatch.setattr(dataspace_identity, "provision_user_identity", _identity)

        rows = await enablement.enable(db, submission)

        assert not [c for c in happy_path if c.startswith("set_member_did")]
        assert rows[EnablementStep.DATASPACE_IDENTITY].status == EnablementStatus.SKIPPED

    async def test_a_refused_did_fails_the_step_closed(
        self, db, submission, monkeypatch, happy_path
    ):
        """A member left without a DID is invisible to every consent-driven
        export, which is the same class of failure as a member who does not
        exist — so it blocks approval rather than being logged past."""

        async def _set_did(sub, *, member_key, did):
            raise ValueError("did 'did:web:member' already belongs to another member")

        monkeypatch.setattr(rec_registry, "set_member_did", _set_did)

        with pytest.raises(EnablementError) as exc:
            await enablement.enable(db, submission)

        assert exc.value.step == EnablementStep.DATASPACE_IDENTITY
        rows = await enablement.load_steps(db, submission.id)
        assert rows[EnablementStep.DATASPACE_IDENTITY].status == EnablementStatus.FAILED
        assert (
            "already belongs to another member"
            in rows[EnablementStep.DATASPACE_IDENTITY].last_error
        )

    async def test_a_retry_of_the_step_alone_still_finds_the_member(
        self, db, submission, monkeypatch, happy_path
    ):
        """The member key is read from step 2's row rather than threaded through
        arguments, so repairing step 3 on its own does not need step 2 to run
        again."""
        attempts: list[str] = []

        async def _set_did(sub, *, member_key, did):
            attempts.append(member_key)
            if len(attempts) == 1:
                raise ValueError("registry unreachable")
            return f"registry member {member_key} holds the dataspace DID"

        monkeypatch.setattr(rec_registry, "set_member_did", _set_did)

        with pytest.raises(EnablementError):
            await enablement.enable(db, submission)

        rows = await enablement.retry(db, submission, step=EnablementStep.DATASPACE_IDENTITY)

        assert attempts == ["member-key-1", "member-key-1"]
        assert rows[EnablementStep.DATASPACE_IDENTITY].status == EnablementStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# Failure
# ---------------------------------------------------------------------------


class TestFailClosed:
    @pytest.fixture()
    def registry_fails(self, monkeypatch, happy_path):
        async def _boom(sub, *, keycloak_username=None):
            raise ValueError("registry said no")

        monkeypatch.setattr(rec_registry, "register_member", _boom)

    async def test_raises_so_approval_does_not_complete(self, db, submission, registry_fails):
        with pytest.raises(EnablementError) as exc:
            await enablement.enable(db, submission)
        assert exc.value.step == EnablementStep.REC_REGISTRY_MEMBER
        assert "registry said no" in str(exc.value)

    async def test_the_failure_is_recorded_not_just_raised(self, db, submission, registry_fails):
        with pytest.raises(EnablementError):
            await enablement.enable(db, submission)

        rows = await enablement.load_steps(db, submission.id)
        row = rows[EnablementStep.REC_REGISTRY_MEMBER]
        assert row.status == EnablementStatus.FAILED
        assert "registry said no" in row.last_error
        assert row.attempts == 1

    async def test_earlier_successes_are_kept(self, db, submission, registry_fails):
        """They really happened.

        Forgetting the Keycloak user locally would orphan it remotely, and the
        next attempt would create a second one. Keeping it is what makes the
        retry idempotent.
        """
        with pytest.raises(EnablementError):
            await enablement.enable(db, submission)

        rows = await enablement.load_steps(db, submission.id)
        assert rows[EnablementStep.KEYCLOAK_USER].status == EnablementStatus.SUCCEEDED
        assert rows[EnablementStep.KEYCLOAK_USER].external_ref == "kc-123"

    async def test_later_steps_do_not_run(self, db, submission, registry_fails, happy_path):
        with pytest.raises(EnablementError):
            await enablement.enable(db, submission)

        assert "dataspace_identity" not in [c.split("(")[0] for c in happy_path]
        rows = await enablement.load_steps(db, submission.id)
        assert rows[EnablementStep.DATASPACE_IDENTITY].status == EnablementStatus.PENDING

    async def test_state_is_failed(self, db, submission, registry_fails):
        with pytest.raises(EnablementError):
            await enablement.enable(db, submission)
        rows = await enablement.load_steps(db, submission.id)
        assert enablement.state_of(rows) == "failed"


class TestAFailedApprovalInvitesNobody:
    """celine-eu/onboarding#8: the invitation waits until approval cannot fail.

    Step 1 used to ask the upsert for the invitation, so an approval that then
    failed at the registry had already emailed the applicant a link to set a
    password on an enabled login. Disabling that login afterwards is not possible
    through the provisioning seam: revocation resolves the member through the
    registry, and the registry step is the one that failed.
    """

    @pytest.fixture()
    def invites(self, monkeypatch, happy_path):
        sent: list[str] = []

        async def _invite(sub):
            sent.append(sub.ref)
            happy_path.append("invitation")
            return "sent"

        monkeypatch.setattr(provisioning, "invite_participant", _invite)
        return sent

    async def test_a_registry_failure_sends_no_invitation(
        self, db, submission, invites, monkeypatch
    ):
        async def _boom(sub, *, keycloak_username=None):
            raise ValueError("registry said no")

        monkeypatch.setattr(rec_registry, "register_member", _boom)

        with pytest.raises(EnablementError):
            await enablement.enable(db, submission)

        assert invites == []
        row = (await enablement.load_steps(db, submission.id))[EnablementStep.KEYCLOAK_USER]
        assert row.status == EnablementStatus.SUCCEEDED
        assert row.invitation == "not_requested"
        assert row.detail == (
            "created, invitation not sent yet: it goes out when approval completes"
        )

    async def test_a_dataspace_identity_failure_sends_no_invitation(
        self, db, submission, invites, monkeypatch
    ):
        async def _boom(sub, **kwargs):
            raise ValueError("identity registry said no")

        monkeypatch.setattr(dataspace_identity, "provision_user_identity", _boom)

        with pytest.raises(EnablementError):
            await enablement.enable(db, submission)

        assert invites == []

    async def test_the_invitation_follows_every_fail_closed_step(
        self, db, submission, invites, happy_path
    ):
        rows = await enablement.enable(db, submission)

        assert invites == [submission.ref]
        calls = [c.split("(")[0] for c in happy_path]
        assert calls.index("invitation") > calls.index("set_member_did")
        assert rows[EnablementStep.KEYCLOAK_USER].invitation == "sent"
        assert rows[EnablementStep.KEYCLOAK_USER].detail == "created, invitation sent"

    async def test_approving_again_once_the_registry_answers_sends_it_once(
        self, db, submission, invites, monkeypatch
    ):
        async def _boom(sub, *, keycloak_username=None):
            raise ValueError("registry said no")

        monkeypatch.setattr(rec_registry, "register_member", _boom)
        with pytest.raises(EnablementError):
            await enablement.enable(db, submission)

        async def _ok(sub, *, keycloak_username=None):
            return "member-key-1"

        monkeypatch.setattr(rec_registry, "register_member", _ok)
        await enablement.enable(db, submission)
        await enablement.enable(db, submission)
        await enablement.retry(db, submission)

        assert invites == [submission.ref]

    async def test_a_skipped_fail_closed_step_does_not_hold_it_back(
        self, db, submission, invites, monkeypatch
    ):
        """A community with no dataspace binding skips step 3; that is complete."""

        async def _identity(sub, **kwargs):
            pass

        monkeypatch.setattr(dataspace_identity, "provision_user_identity", _identity)
        rows = await enablement.enable(db, submission)

        assert rows[EnablementStep.DATASPACE_IDENTITY].status == EnablementStatus.SKIPPED
        assert invites == [submission.ref]

    async def test_a_soft_failure_does_not_hold_it_back(self, db, submission, invites, monkeypatch):
        async def _boom(sub, **kwargs):
            raise ValueError("connector down")

        monkeypatch.setattr(dataspace_identity, "provision_user_shares", _boom)
        rows = await enablement.enable(db, submission)

        assert rows[EnablementStep.DATASPACE_SHARE].status == EnablementStatus.FAILED
        assert invites == [submission.ref]

    async def test_no_login_means_no_invitation(self, db, submission, invites, monkeypatch):
        monkeypatch.setattr(provisioning, "provisioning_enabled", lambda: False)
        rows = await enablement.enable(db, submission)

        assert rows[EnablementStep.KEYCLOAK_USER].status == EnablementStatus.SKIPPED
        assert invites == []

    async def test_a_retry_naming_another_step_sends_nothing(
        self, db, submission, invites, monkeypatch
    ):
        """The one way to reach "every step done, invitation still due": the stage
        itself never ran. A retry named for the consent step is about that step."""
        rows = await enablement.ensure_rows(db, submission)
        for step, row in rows.items():
            row.status = EnablementStatus.SUCCEEDED
        rows[EnablementStep.KEYCLOAK_USER].invitation = "not_requested"
        rows[EnablementStep.KEYCLOAK_USER].detail = "created, invitation not sent yet"

        await enablement.retry(db, submission, step=EnablementStep.DATASPACE_SHARE)
        assert invites == []

        await enablement.retry(db, submission)
        assert invites == [submission.ref]

    @pytest.mark.parametrize(
        "error",
        [
            ValueError("Provisioning refused sending the invitation (404 member_not_found)"),
            ConfigurationError("PROVISIONING_URL is required"),
            RuntimeError("connection reset"),
        ],
    )
    async def test_an_invitation_that_cannot_be_sent_is_a_retryable_send_failed(
        self, db, submission, happy_path, monkeypatch, error
    ):
        """Never a failed approval: every fail-closed step has succeeded by now."""

        async def _invite(sub):
            raise error

        monkeypatch.setattr(provisioning, "invite_participant", _invite)
        rows = await enablement.enable(db, submission)

        row = rows[EnablementStep.KEYCLOAK_USER]
        assert row.status == EnablementStatus.SUCCEEDED
        assert row.invitation == "send_failed"
        assert row.detail == "created, invitation not sent: the email could not be sent"
        assert enablement.state_of(rows) == "complete"

    async def test_the_approval_itself_completes_and_the_blocked_one_does_not(
        self, db, invites, monkeypatch, happy_path, seed_rec
    ):
        """The issue's scenario, through `review.transition`."""
        from celine.onboarding.services import audit_service, review
        from celine.onboarding.services.audit_service import Actor

        seed_rec("rec-a", steps=["consents", "review"])

        async def _record_and_commit(db, **kwargs):
            pass

        monkeypatch.setattr(audit_service, "record_and_commit", _record_and_commit)
        monkeypatch.setattr(review.audit_service, "record", lambda db, **kwargs: None)

        async def _boom(sub, *, keycloak_username=None):
            raise ValueError("registry unreachable")

        monkeypatch.setattr(rec_registry, "register_member", _boom)
        submission = FakeSubmission(status=SubmissionStatus.UNDER_REVIEW, verification=OFFLINE)

        with pytest.raises(EnablementError):
            await review.transition(
                db, submission, SubmissionStatus.APPROVED, actor=Actor.system("test")
            )
        assert submission.status == SubmissionStatus.UNDER_REVIEW
        assert invites == []

        async def _ok(sub, *, keycloak_username=None):
            return "member-key-1"

        monkeypatch.setattr(rec_registry, "register_member", _ok)
        await review.transition(
            db, submission, SubmissionStatus.APPROVED, actor=Actor.system("test")
        )
        assert submission.status == SubmissionStatus.APPROVED
        assert invites == [submission.ref]


class TestAMisconfiguredStep:
    """A deployment's own fault is not the reviewing operator's to read.

    The message names settings only a platform operator can change, and it is
    shown in the console to whoever pressed Approve. They are told who can act;
    the detail goes to the log, where that person is looking.
    """

    @pytest.fixture()
    def keycloak_unconfigured(self, monkeypatch, happy_path):
        async def _boom(sub):
            raise ConfigurationError("PROVISIONING_URL is required")

        monkeypatch.setattr(provisioning, "provision_participant", _boom)

    async def test_it_still_fails_closed(self, db, submission, keycloak_unconfigured):
        with pytest.raises(EnablementError) as exc:
            await enablement.enable(db, submission)
        assert exc.value.step == EnablementStep.KEYCLOAK_USER

    async def test_the_operator_is_told_who_can_fix_it(self, db, submission, keycloak_unconfigured):
        with pytest.raises(EnablementError):
            await enablement.enable(db, submission)

        row = (await enablement.load_steps(db, submission.id))[EnablementStep.KEYCLOAK_USER]
        assert "not configured in this deployment" in row.last_error
        assert "platform operator" in row.last_error

    async def test_the_setting_is_not_named_to_the_operator(
        self, db, submission, keycloak_unconfigured
    ):
        with pytest.raises(EnablementError) as exc:
            await enablement.enable(db, submission)

        row = (await enablement.load_steps(db, submission.id))[EnablementStep.KEYCLOAK_USER]
        assert "PROVISIONING_URL" not in row.last_error
        assert "PROVISIONING_URL" not in str(exc.value)

    async def test_the_detail_is_logged(self, db, submission, keycloak_unconfigured, caplog):
        with caplog.at_level("ERROR"), pytest.raises(EnablementError):
            await enablement.enable(db, submission)

        assert "PROVISIONING_URL" in caplog.text


class TestSoftFailure:
    async def test_share_failure_does_not_block(self, db, submission, monkeypatch, happy_path):
        """A missing consent row is recoverable and has a retry; approval stands."""

        async def _boom(sub, **kwargs):
            raise ValueError("connector refused")

        monkeypatch.setattr(dataspace_identity, "provision_user_shares", _boom)

        rows = await enablement.enable(db, submission)  # no raise

        assert rows[EnablementStep.DATASPACE_SHARE].status == EnablementStatus.FAILED
        assert "connector refused" in rows[EnablementStep.DATASPACE_SHARE].last_error
        assert rows[EnablementStep.DATASPACE_IDENTITY].status == EnablementStatus.SUCCEEDED
        assert enablement.state_of(rows) == "failed"


# ---------------------------------------------------------------------------
# Not applicable is not failure
# ---------------------------------------------------------------------------


class TestSkipping:
    async def test_unbound_community_skips_rather_than_fails(
        self, db, submission, monkeypatch, happy_path
    ):
        async def _none(sub, *, keycloak_username=None):
            return None

        monkeypatch.setattr(rec_registry, "register_member", _none)

        rows = await enablement.enable(db, submission)
        row = rows[EnablementStep.REC_REGISTRY_MEMBER]
        assert row.status == EnablementStatus.SKIPPED
        assert "no rec_registry binding" in row.detail

    async def test_no_sharing_consent_skips_the_share(self, db, monkeypatch, happy_path):
        submission = FakeSubmission(data_sharing_consent=False)
        rows = await enablement.enable(db, submission)
        assert rows[EnablementStep.DATASPACE_SHARE].status == EnablementStatus.SKIPPED

    async def test_skipped_counts_as_complete(self, db, monkeypatch, happy_path):
        """ "Nothing to do" is done — a community with no dataspace is not broken."""
        submission = FakeSubmission(data_sharing_consent=False)

        async def _identity(sub, **kwargs):
            pass  # leaves dataspace_vc_id unset

        monkeypatch.setattr(dataspace_identity, "provision_user_identity", _identity)

        rows = await enablement.enable(db, submission)
        assert enablement.state_of(rows) == "complete"


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


class TestRetry:
    @pytest.fixture()
    async def after_failure(self, db, submission, monkeypatch, happy_path):
        async def _boom(sub, *, keycloak_username=None):
            raise ValueError("registry said no")

        monkeypatch.setattr(rec_registry, "register_member", _boom)
        with pytest.raises(EnablementError):
            await enablement.enable(db, submission)
        happy_path.clear()
        return happy_path

    async def test_does_not_rerun_succeeded_steps(self, db, submission, after_failure, monkeypatch):
        """Retry means finish what is unfinished, not do it all again."""

        async def _ok(sub, *, keycloak_username=None):
            after_failure.append("rec_registry_member")
            return "member-key-1"

        monkeypatch.setattr(rec_registry, "register_member", _ok)

        rows = await enablement.retry(db, submission)
        assert "keycloak_user" not in after_failure
        assert enablement.state_of(rows) == "complete"

    async def test_counts_a_second_attempt(self, db, submission, after_failure, monkeypatch):
        async def _ok(sub, *, keycloak_username=None):
            return "member-key-1"

        monkeypatch.setattr(rec_registry, "register_member", _ok)
        rows = await enablement.retry(db, submission)
        assert rows[EnablementStep.REC_REGISTRY_MEMBER].attempts == 2
        assert rows[EnablementStep.KEYCLOAK_USER].attempts == 1

    async def test_clears_the_previous_error(self, db, submission, after_failure, monkeypatch):
        async def _ok(sub, *, keycloak_username=None):
            return "member-key-1"

        monkeypatch.setattr(rec_registry, "register_member", _ok)
        rows = await enablement.retry(db, submission)
        assert rows[EnablementStep.REC_REGISTRY_MEMBER].last_error is None

    async def test_never_raises_even_when_it_fails_again(self, db, submission, after_failure):
        """The submission is already approved — there is no decision left to block.

        The operator asked to repair; the answer is the step rows.
        """
        rows = await enablement.retry(db, submission)
        assert rows[EnablementStep.REC_REGISTRY_MEMBER].status == EnablementStatus.FAILED
        assert rows[EnablementStep.REC_REGISTRY_MEMBER].attempts == 2

    async def test_one_named_step_only(self, db, submission, after_failure, monkeypatch):
        async def _ok(sub, *, keycloak_username=None):
            return "member-key-1"

        monkeypatch.setattr(rec_registry, "register_member", _ok)

        rows = await enablement.retry(db, submission, step=EnablementStep.DATASPACE_IDENTITY)
        # The registry step was not the one asked for, so it stays failed.
        assert rows[EnablementStep.REC_REGISTRY_MEMBER].status == EnablementStatus.FAILED
        assert rows[EnablementStep.DATASPACE_IDENTITY].status == EnablementStatus.SUCCEEDED

    async def test_unknown_step_is_rejected(self, db, submission, happy_path):
        with pytest.raises(KeyError, match="Unknown enablement step"):
            await enablement.retry(db, submission, step="teleport")


class TestASucceededShareIsReExaminedByName:
    """A split can open under a `succeeded` share step (the maintainer, 2026-09-19).

    A member's withdrawal that lands at one connector of two leaves step 4 as it
    was, so a retry that skipped a succeeded step could never reach it. Naming the
    step re-runs it: its run reads every connector and writes only where they
    disagree with the member's newest decision, so re-running it where nothing is
    split writes nothing. Unnamed — approval again, "retry all" — it is still
    skipped: those finish what is unfinished.
    """

    async def _approved(self, db, submission, happy_path):
        rows = await enablement.enable(db, submission)
        assert rows[EnablementStep.DATASPACE_SHARE].status == EnablementStatus.SUCCEEDED
        happy_path.clear()
        return rows

    async def test_a_named_retry_re_runs_a_succeeded_share(self, db, submission, happy_path):
        await self._approved(db, submission, happy_path)

        rows = await enablement.retry(db, submission, step=EnablementStep.DATASPACE_SHARE)

        assert happy_path == ["dataspace_share"]
        row = rows[EnablementStep.DATASPACE_SHARE]
        assert row.status == EnablementStatus.SUCCEEDED
        assert row.attempts == 2

    async def test_an_unnamed_retry_leaves_it(self, db, submission, happy_path):
        await self._approved(db, submission, happy_path)

        rows = await enablement.retry(db, submission)

        assert happy_path == []
        assert rows[EnablementStep.DATASPACE_SHARE].attempts == 1

    async def test_approval_again_leaves_it(self, db, submission, happy_path):
        await self._approved(db, submission, happy_path)

        await enablement.enable(db, submission)

        assert happy_path == []

    async def test_a_re_examination_that_fails_is_a_failed_step(
        self, db, submission, happy_path, monkeypatch
    ):
        await self._approved(db, submission, happy_path)

        async def _unreadable(sub, **kwargs):
            raise ValueError("could not read what example-dso's connector records")

        monkeypatch.setattr(dataspace_identity, "provision_user_shares", _unreadable)
        rows = await enablement.retry(db, submission, step=EnablementStep.DATASPACE_SHARE)

        row = rows[EnablementStep.DATASPACE_SHARE]
        assert row.status == EnablementStatus.FAILED
        assert "could not read" in row.last_error

    async def test_a_share_skipped_for_a_declined_form_is_re_run_by_name(
        self, db, monkeypatch, happy_path
    ):
        """Reversed on 2026-09-19 (the maintainer): it used to stay skipped.

        A member who declined everything on the form has nothing to write at
        approval, and may grant on their page afterwards — a split made there is
        the retry's to converge like any other. Only a retry naming the step
        reaches it; approval again and "retry all" still leave it.
        """
        submission = FakeSubmission(data_sharing_consent=False)
        rows = await enablement.enable(db, submission)
        assert rows[EnablementStep.DATASPACE_SHARE].status == EnablementStatus.SKIPPED
        happy_path.clear()

        await enablement.enable(db, submission)
        await enablement.retry(db, submission)
        assert happy_path == []

        rows = await enablement.retry(db, submission, step=EnablementStep.DATASPACE_SHARE)

        assert happy_path == ["dataspace_share"]
        assert rows[EnablementStep.DATASPACE_SHARE].status == EnablementStatus.SUCCEEDED

    async def test_a_share_skipped_for_want_of_a_connector_stays_skipped(
        self, db, monkeypatch, happy_path
    ):
        from celine.onboarding.config.settings import settings

        monkeypatch.setattr(settings, "ds_connector_url", "")
        submission = FakeSubmission()
        rows = await enablement.enable(db, submission)
        assert rows[EnablementStep.DATASPACE_SHARE].status == EnablementStatus.SKIPPED
        happy_path.clear()

        rows = await enablement.retry(db, submission, step=EnablementStep.DATASPACE_SHARE)

        assert happy_path == []
        assert rows[EnablementStep.DATASPACE_SHARE].status == EnablementStatus.SKIPPED


class TestAFailedSendIsRetriedByName:
    """`send_failed` is the one `succeeded` step a retry re-runs, and only by name.

    The operator is repairing an email that did not go out. `cooldown` and
    `no_email` are not re-run: the first would be a second email nobody decided
    on, and the second can never be sent. An unnamed retry, like approval, never
    re-sends.

    The re-run is step 1's idempotent upsert, which asks for no invitation, then
    the invitation itself, which is where the codes come from.
    """

    @pytest.fixture()
    def login(self, monkeypatch, happy_path):
        """The invitation answers the queued codes in order, one per call."""
        state = {"codes": [], "calls": 0, "upserts": 0}

        async def _kc(sub):
            state["upserts"] += 1
            return ParticipantProvisionResult(
                user_id="kc-123",
                username=sub.email,
                created=state["upserts"] == 1,
                invitation="not_requested",
            )

        async def _invite(sub):
            state["calls"] += 1
            return state["codes"].pop(0)

        monkeypatch.setattr(provisioning, "provision_participant", _kc)
        monkeypatch.setattr(provisioning, "invite_participant", _invite)
        return state

    async def _approved_with(self, db, submission, login, code):
        login["codes"] = [code]
        rows = await enablement.enable(db, submission)
        assert rows[EnablementStep.KEYCLOAK_USER].status == EnablementStatus.SUCCEEDED
        assert rows[EnablementStep.KEYCLOAK_USER].invitation == code
        assert enablement.state_of(rows) == "complete"
        return rows

    async def test_a_named_retry_sends_again(self, db, submission, login):
        await self._approved_with(db, submission, login, "send_failed")
        login["codes"] = ["sent"]

        rows = await enablement.retry(db, submission, step=EnablementStep.KEYCLOAK_USER)

        assert login["calls"] == 2
        row = rows[EnablementStep.KEYCLOAK_USER]
        assert row.status == EnablementStatus.SUCCEEDED
        assert row.invitation == "sent"
        assert row.attempts == 2
        assert row.detail == "already existed, invitation sent"
        assert row.external_ref == "kc-123"

    async def test_a_second_failure_stays_retryable(self, db, submission, login):
        await self._approved_with(db, submission, login, "send_failed")
        login["codes"] = ["send_failed", "sent"]

        await enablement.retry(db, submission, step=EnablementStep.KEYCLOAK_USER)
        rows = await enablement.retry(db, submission, step=EnablementStep.KEYCLOAK_USER)

        assert login["calls"] == 3
        assert rows[EnablementStep.KEYCLOAK_USER].invitation == "sent"

    async def test_the_named_retry_runs_only_that_step(self, db, submission, login, happy_path):
        await self._approved_with(db, submission, login, "send_failed")
        happy_path.clear()
        login["codes"] = ["sent"]

        rows = await enablement.retry(db, submission, step=EnablementStep.KEYCLOAK_USER)

        assert happy_path == []
        assert rows[EnablementStep.REC_REGISTRY_MEMBER].attempts == 1

    async def test_an_unnamed_retry_does_not_resend(self, db, submission, login):
        await self._approved_with(db, submission, login, "send_failed")

        rows = await enablement.retry(db, submission)

        assert login["calls"] == 1
        assert rows[EnablementStep.KEYCLOAK_USER].invitation == "send_failed"

    async def test_approval_again_does_not_resend(self, db, submission, login):
        await self._approved_with(db, submission, login, "send_failed")

        await enablement.enable(db, submission)

        assert login["calls"] == 1

    @pytest.mark.parametrize(
        "code", ["cooldown", "no_email", "sent", "has_password", "account_disabled"]
    )
    async def test_no_other_code_is_rerun_by_name(self, db, submission, login, code):
        await self._approved_with(db, submission, login, code)

        rows = await enablement.retry(db, submission, step=EnablementStep.KEYCLOAK_USER)

        assert login["calls"] == 1
        assert login["upserts"] == 1
        assert rows[EnablementStep.KEYCLOAK_USER].invitation == code
        assert rows[EnablementStep.KEYCLOAK_USER].attempts == 1

    async def test_a_resend_that_raises_fails_the_step_and_can_be_retried(
        self, db, submission, login, monkeypatch
    ):
        await self._approved_with(db, submission, login, "send_failed")

        async def _outage(sub):
            raise ValueError("Provisioning refused provisioning a login (502)")

        monkeypatch.setattr(provisioning, "provision_participant", _outage)
        rows = await enablement.retry(db, submission, step=EnablementStep.KEYCLOAK_USER)

        row = rows[EnablementStep.KEYCLOAK_USER]
        assert row.status == EnablementStatus.FAILED
        # The account is still recorded, so the steps after it keep their reference.
        assert row.external_ref == "kc-123"
        # And no invitation was attempted on a step that did not succeed.
        assert login["calls"] == 1


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


class TestRevoke:
    @pytest.fixture()
    def revocations(self, monkeypatch):
        done: list[str] = []

        async def _disable_kc(sub):
            # Addressed by `(community, ref)`, not by the recorded uuid — the
            # provisioning service's revocation route takes the member key.
            done.append(f"keycloak_user:{sub.ref}")
            return "disabled login member@example.org"

        async def _deactivate(sub, *, member_key):
            done.append(f"rec_registry_member:{member_key}")
            return "deactivated"

        async def _revoke_identity(sub):
            done.append("dataspace_identity")
            sub.dataspace_vc_id = None
            return "revoked"

        async def _withdraw(sub, **kwargs):
            # Nothing stood. A test about the withdrawal replaces this; left
            # real, it would fail on the fake submission and hold the identity
            # step back, which is a behaviour of its own (tested below).
            return True

        monkeypatch.setattr(dataspace_identity, "withdraw_user_shares", _withdraw)
        # No identity registry to sweep for further credentials: the recorded one
        # is the whole of it here (the sweep has its own test below).
        monkeypatch.setattr(dataspace_identity.settings, "identity_registry_url", "")
        monkeypatch.setattr(provisioning, "disable_participant", _disable_kc)
        monkeypatch.setattr(rec_registry, "deactivate_member", _deactivate)
        monkeypatch.setattr(dataspace_identity, "revoke_user_identity", _revoke_identity)
        return done

    async def test_the_login_is_closed_before_the_member_is_deactivated(
        self, db, submission, happy_path, revocations
    ):
        """The one place the order is **not** the reverse of the pipeline.

        Revoking a login resolves `(community, key)` through the registry
        export, and that export carries only `active` members. Deactivating the
        member first makes the very next call a 404: the login survives, the
        participant can still sign in, and the step row says the revocation
        succeeded because nothing failed.
        """
        await enablement.enable(db, submission)
        await enablement.revoke(db, submission)

        assert [d.split(":")[0] for d in revocations] == [
            "dataspace_identity",
            "keycloak_user",
            "rec_registry_member",
        ]

    async def test_every_step_is_revoked_exactly_once(self):
        assert set(enablement.REVOKE_ORDER) == {spec.step for spec in enablement.PIPELINE}
        assert len(enablement.REVOKE_ORDER) == len(enablement.PIPELINE)

    async def test_passes_the_recorded_references(self, db, submission, happy_path, revocations):
        await enablement.enable(db, submission)
        await enablement.revoke(db, submission)
        # The login is addressed by the member key, the registry member by the
        # reference the step row recorded.
        assert "keycloak_user:20260730-test" in revocations
        assert "rec_registry_member:member-key-1" in revocations

    async def test_sharing_consent_is_withdrawn_here(
        self, db, submission, happy_path, revocations, monkeypatch
    ):
        """This step grants on the person's behalf, so it withdraws on theirs.

        It used to do nothing, on the reasoning that withdrawal is the subject's
        own act. That holds for a person *choosing* to stop sharing; it does not
        hold here, where the community removed them and the same sequence deletes
        the credential they would have withdrawn with. The consent stood and its
        subject had no way left to reach it.
        """
        called: list[str] = []

        async def _withdraw(sub, **kwargs):
            called.append(sub.ref)
            return True

        monkeypatch.setattr(dataspace_identity, "withdraw_user_shares", _withdraw)

        await enablement.enable(db, submission)
        rows = await enablement.revoke(db, submission)

        assert called == [submission.ref]
        # A revoked step goes back to PENDING — the same state the other steps
        # land in, and what `test_revoked_steps_become_retriable_again` relies on.
        assert rows[EnablementStep.DATASPACE_SHARE].status == EnablementStatus.PENDING
        assert rows[EnablementStep.DATASPACE_SHARE].detail == (
            "no grant this community collected stood at any connector"
        )

    async def test_the_share_is_withdrawn_before_the_identity_that_carried_it(
        self, db, submission, happy_path, revocations, monkeypatch
    ):
        """Ordering, and it is free: `revoke` walks `reversed(PIPELINE)`.

        Asserted rather than assumed, because the two steps are adjacent and a
        later reordering of the pipeline would silently swap them — leaving the
        withdrawal to be attempted for a DID whose credential is already gone.
        """
        order: list[str] = []

        async def _withdraw(sub, **kwargs):
            order.append("share")
            return True

        original = dataspace_identity.revoke_user_identity

        async def _revoke_identity(sub):
            order.append("identity")
            return await original(sub)

        monkeypatch.setattr(dataspace_identity, "withdraw_user_shares", _withdraw)
        monkeypatch.setattr(dataspace_identity, "revoke_user_identity", _revoke_identity)

        await enablement.enable(db, submission)
        await enablement.revoke(db, submission)

        assert order == ["share", "identity"]

    async def test_revoked_steps_become_retriable_again(
        self, db, submission, happy_path, revocations
    ):
        await enablement.enable(db, submission)
        rows = await enablement.revoke(db, submission)
        assert rows[EnablementStep.KEYCLOAK_USER].status == EnablementStatus.PENDING
        assert rows[EnablementStep.KEYCLOAK_USER].external_ref is None

    async def test_a_revoked_login_forgets_its_invitation(
        self, db, submission, happy_path, revocations, monkeypatch
    ):
        """The code described access that no longer exists; a re-approval records its own."""

        async def _kc(sub):
            return ParticipantProvisionResult("kc-123", sub.email, True, invitation="sent")

        monkeypatch.setattr(provisioning, "provision_participant", _kc)
        await enablement.enable(db, submission)
        rows = await enablement.revoke(db, submission)
        assert rows[EnablementStep.KEYCLOAK_USER].invitation is None

    async def test_a_failed_revocation_is_recorded_and_the_rest_continues(
        self, db, submission, happy_path, revocations, monkeypatch
    ):
        """A half-done revocation must leave a record of what is still out there.

        That record is the only way anybody finds the rest.
        """
        await enablement.enable(db, submission)

        async def _boom(sub, *, member_key):
            raise ValueError("registry unreachable")

        monkeypatch.setattr(rec_registry, "deactivate_member", _boom)

        rows = await enablement.revoke(db, submission)
        assert rows[EnablementStep.REC_REGISTRY_MEMBER].status == EnablementStatus.FAILED
        assert "registry unreachable" in rows[EnablementStep.REC_REGISTRY_MEMBER].last_error
        # ...and the login, which the order now puts *before* this step, was
        # already closed. That is the property worth having: a registry that
        # cannot be reached leaves a stale member row, not somebody who was
        # removed from their community and can still sign in.
        assert "keycloak_user:20260730-test" in revocations

    async def test_a_share_that_landed_only_in_part_is_still_withdrawn(
        self, db, submission, happy_path, revocations, monkeypatch
    ):
        """One offer is recorded at every connector holding its data (ADR-0007).

        So a `failed` share step can have granted at one connector before
        another refused. Revoking only a `succeeded` step left that half standing
        after the membership it belonged to was gone.
        """

        async def _half(sub, **kwargs):
            raise ValueError("Share provisioning failed: research: example-dso's connector: 502")

        withdrawn: list[str] = []

        async def _withdraw(sub, **kwargs):
            withdrawn.append(sub.ref)
            return True

        monkeypatch.setattr(dataspace_identity, "provision_user_shares", _half)
        monkeypatch.setattr(dataspace_identity, "withdraw_user_shares", _withdraw)

        await enablement.enable(db, submission)
        rows = await enablement.load_steps(db, submission.id)
        assert rows[EnablementStep.DATASPACE_SHARE].status == EnablementStatus.FAILED

        rows = await enablement.revoke(db, submission)

        assert withdrawn == [submission.ref]
        assert rows[EnablementStep.DATASPACE_SHARE].status == EnablementStatus.PENDING

    async def test_a_refused_withdrawal_is_a_failed_revocation_not_nothing_to_do(
        self, db, submission, happy_path, revocations, monkeypatch
    ):
        """It used to read "nothing to withdraw" — a consent outliving the
        membership, filed as success."""

        async def _refused(sub, *, raise_on_error=False, **kwargs):
            if raise_on_error:
                raise ValueError("Share withdrawal failed: research: example-dso's connector: 502")
            return False

        monkeypatch.setattr(dataspace_identity, "withdraw_user_shares", _refused)

        await enablement.enable(db, submission)
        rows = await enablement.revoke(db, submission)

        row = rows[EnablementStep.DATASPACE_SHARE]
        assert row.status == EnablementStatus.FAILED
        assert "example-dso" in row.last_error

    async def test_a_member_who_declined_the_form_is_still_withdrawn(
        self, db, submission, happy_path, revocations, monkeypatch
    ):
        """Step 4 was skipped at approval, and the member granted on their page since.

        Revocation withdraws every grant the community collected, from the form
        or the page (the maintainer, 2026-09-19), so the step's status is not
        what decides: a member holding a dataspace identity may hold grants.
        """
        submission.data_sharing_consent = False
        withdrawn: list[str] = []

        async def _withdraw(sub, *, report=None, **kwargs):
            withdrawn.append(sub.ref)
            if report is not None:
                report.append("research withdrawn at this community's connector")
            return True

        monkeypatch.setattr(dataspace_identity, "withdraw_user_shares", _withdraw)

        await enablement.enable(db, submission)
        rows = await enablement.load_steps(db, submission.id)
        assert rows[EnablementStep.DATASPACE_SHARE].status == EnablementStatus.SKIPPED

        rows = await enablement.revoke(db, submission)

        assert withdrawn == [submission.ref]
        assert rows[EnablementStep.DATASPACE_SHARE].status == EnablementStatus.PENDING
        assert "research withdrawn" in rows[EnablementStep.DATASPACE_SHARE].detail

    async def test_the_identity_waits_for_a_withdrawal_that_failed(
        self, db, submission, happy_path, revocations, monkeypatch
    ):
        """The identity is what the withdrawal needs, so it is kept until the withdrawal lands.

        ds admits the community's read and write for a subject only while they
        are a member of its organisation, and the identity step deletes that
        membership and clears the DID. Run after a failed withdrawal, it left a
        second revocation nothing to withdraw with — the grant outlived the
        membership with no way left to reach it.
        """
        attempts: list[str] = []

        async def _withdraw(sub, *, raise_on_error=False, **kwargs):
            attempts.append(sub.dataspace_did)
            if len(attempts) == 1:
                raise ValueError(
                    "Share withdrawal failed: could not read what example-dso's connector records"
                )
            return True

        monkeypatch.setattr(dataspace_identity, "withdraw_user_shares", _withdraw)

        await enablement.enable(db, submission)
        rows = await enablement.revoke(db, submission)

        # Not revoked: still succeeded, the DID still on the submission.
        assert "dataspace_identity" not in revocations
        assert rows[EnablementStep.DATASPACE_IDENTITY].status == EnablementStatus.SUCCEEDED
        assert submission.dataspace_did == "did:web:member"
        share = rows[EnablementStep.DATASPACE_SHARE]
        assert share.status == EnablementStatus.FAILED
        assert "identity is kept" in share.last_error
        # Everything that does not depend on it still ran.
        assert "keycloak_user:20260730-test" in revocations
        assert enablement.state_of(rows) == "failed"

        # Revoking again is the retry: the withdrawal, then the identity.
        rows = await enablement.revoke(db, submission)

        assert attempts == ["did:web:member", "did:web:member"]
        assert rows[EnablementStep.DATASPACE_SHARE].status == EnablementStatus.PENDING
        assert "dataspace_identity" in revocations
        assert rows[EnablementStep.DATASPACE_IDENTITY].status == EnablementStatus.PENDING

    async def test_without_a_dataspace_identity_there_is_nothing_to_withdraw_for(
        self, db, submission, revocations, monkeypatch
    ):
        async def _withdraw(sub, **kwargs):  # pragma: no cover — reaching it is the failure
            raise AssertionError("no subject to withdraw for")

        monkeypatch.setattr(dataspace_identity, "withdraw_user_shares", _withdraw)
        rows = await enablement.revoke(db, submission)
        assert rows[EnablementStep.DATASPACE_SHARE].status == EnablementStatus.PENDING

    async def test_nothing_to_revoke_is_not_an_error(self, db, submission, revocations):
        rows = await enablement.revoke(db, submission)
        assert enablement.state_of(rows) == "not_started"
        assert revocations == []

    async def test_the_member_waits_for_a_login_release_that_failed(
        self, db, submission, happy_path, revocations, monkeypatch
    ):
        """Releasing the login resolves the member through the registry. A member
        deactivated while that failed was a login nobody here could release any
        more; so the member waits, and revoking again releases both, in order."""
        calls: list[str] = []

        async def _disable(sub):
            calls.append(sub.ref)
            if len(calls) == 1:
                raise ValueError("Provisioning refused revoking a login (502 provisioning_failed)")
            return "released login member@example.org"

        monkeypatch.setattr(provisioning, "disable_participant", _disable)

        await enablement.enable(db, submission)
        rows = await enablement.revoke(db, submission)

        assert not any(d.startswith("rec_registry_member") for d in revocations)
        assert rows[EnablementStep.REC_REGISTRY_MEMBER].status == EnablementStatus.SUCCEEDED
        assert rows[EnablementStep.KEYCLOAK_USER].status == EnablementStatus.FAILED

        rows = await enablement.revoke(db, submission)

        assert len(calls) == 2
        assert rows[EnablementStep.KEYCLOAK_USER].status == EnablementStatus.PENDING
        assert "rec_registry_member:member-key-1" in revocations
        assert enablement.revoked(rows)

    async def test_a_step_whose_revocation_failed_is_revoked_again(
        self, db, submission, happy_path, revocations, monkeypatch
    ):
        """Revoking again is the retry for a failed revocation too:
        the row is `failed`, not `succeeded`, and used to be skipped for good."""
        attempts: list[str] = []

        async def _deactivate(sub, *, member_key):
            attempts.append(member_key)
            if len(attempts) == 1:
                raise ValueError("REC registry refused to deactivate member (503)")
            return "deactivated"

        monkeypatch.setattr(rec_registry, "deactivate_member", _deactivate)

        await enablement.enable(db, submission)
        rows = await enablement.revoke(db, submission)
        assert rows[EnablementStep.REC_REGISTRY_MEMBER].status == EnablementStatus.FAILED

        rows = await enablement.revoke(db, submission)

        assert attempts == ["member-key-1", "member-key-1"]
        assert rows[EnablementStep.REC_REGISTRY_MEMBER].status == EnablementStatus.PENDING

    async def test_every_credential_the_community_linked_is_revoked_and_the_id_forgotten(
        self, db, submission, happy_path, revocations, monkeypatch
    ):
        """ds moves the login to the next community's DID only once the old one
        holds no active credential (ADR-0028); the sharing page may have issued
        one after approval. And a re-approval mints a new subject id."""
        from celine.onboarding.services import member_release, template_service

        monkeypatch.setattr(dataspace_identity.settings, "identity_registry_url", "http://ir")
        held = [["urn:uuid:recorded", "urn:uuid:later"], []]
        swept: list[tuple] = []

        async def _held(identifiers, did, binding):
            swept.append((tuple(identifiers.items()), did))
            return member_release.HeldCredentials(ours=held.pop(0))

        async def _revoke_credential(credential_id, binding):
            revocations.append(f"credential:{credential_id}")

        async def _fresh():
            pass

        monkeypatch.setattr(member_release, "held_credentials", _held)
        monkeypatch.setattr(member_release, "revoke_credential", _revoke_credential)
        monkeypatch.setattr(template_service, "ensure_fresh", _fresh)
        monkeypatch.setattr(
            template_service,
            "dataspace_binding",
            lambda slug: SimpleNamespace(organization="rec-a", linked_participant_did=""),
        )

        await enablement.enable(db, submission)
        submission.dataspace_vc_id = "urn:uuid:recorded"
        submission.dataspace_subject_id = "old-id"
        rows = await enablement.revoke(db, submission)

        assert "credential:urn:uuid:later" in revocations
        assert "credential:urn:uuid:recorded" not in revocations  # the recorded path did it
        assert swept[0] == ((("email", "member@example.org"),), "did:web:member")
        assert submission.dataspace_subject_id is None
        assert rows[EnablementStep.DATASPACE_IDENTITY].status == EnablementStatus.PENDING

    async def test_a_step_that_failed_to_run_is_not_revoked(
        self, db, submission, happy_path, revocations, monkeypatch
    ):
        """Only a failed *revocation* is retried; a run that failed undid nothing
        to begin with, and its reason stays on the row."""
        await enablement.enable(db, submission)
        rows = await enablement.load_steps(db, submission.id)
        row = rows[EnablementStep.REC_REGISTRY_MEMBER]
        row.status = EnablementStatus.FAILED
        row.last_error = "ValueError: REC registry refused member (409)"

        await enablement.revoke(db, submission)

        assert not any(d.startswith("rec_registry_member") for d in revocations)
        assert row.last_error == "ValueError: REC registry refused member (409)"


# ---------------------------------------------------------------------------
# Summary state
# ---------------------------------------------------------------------------


class TestStateOf:
    def _rows(self, *statuses):
        return {
            spec.step: SubmissionEnablementStep(step=spec.step, status=status)
            for spec, status in zip(enablement.PIPELINE, statuses)
        }

    def test_no_rows_is_not_started(self):
        assert enablement.state_of({}) == "not_started"

    def test_all_pending_is_not_started(self):
        assert enablement.state_of(self._rows(*["pending"] * 4)) == "not_started"

    def test_all_succeeded_is_complete(self):
        assert enablement.state_of(self._rows(*["succeeded"] * 4)) == "complete"

    def test_mixed_succeeded_and_skipped_is_complete(self):
        rows = self._rows("succeeded", "skipped", "skipped", "succeeded")
        assert enablement.state_of(rows) == "complete"

    def test_partially_run_is_partial(self):
        rows = self._rows("succeeded", "pending", "pending", "pending")
        assert enablement.state_of(rows) == "partial"

    def test_any_failure_wins(self):
        """An operator scanning a queue needs the thing that needs them."""
        rows = self._rows("succeeded", "failed", "pending", "pending")
        assert enablement.state_of(rows) == "failed"


# ---------------------------------------------------------------------------
# Approval, end to end through the review service
# ---------------------------------------------------------------------------


class TestApprovalRecordsTheAttempt:
    """A blocked approval must still be attributable to whoever tried it.

    The step rows say what broke; only the audit trail says who tried. "Nobody
    ever tried to approve this" is a different fact from "somebody tried and the
    registry was down".
    """

    @pytest.fixture()
    def review_env(self, monkeypatch, happy_path, seed_rec):
        from celine.onboarding.services import audit_service, review

        seed_rec("rec-a", steps=["consents", "review"])

        recorded: list[dict] = []

        async def _record_and_commit(db, **kwargs):
            recorded.append(kwargs)

        def _record(db, **kwargs):
            recorded.append(kwargs)

        monkeypatch.setattr(audit_service, "record_and_commit", _record_and_commit)
        monkeypatch.setattr(review.audit_service, "record", _record)
        return review, recorded

    async def test_a_blocked_approval_is_audited_and_leaves_the_status(
        self, db, review_env, monkeypatch
    ):
        review, recorded = review_env
        submission = FakeSubmission(status=SubmissionStatus.UNDER_REVIEW, verification=OFFLINE)

        async def _boom(sub, *, keycloak_username=None):
            raise ValueError("registry unreachable")

        monkeypatch.setattr(rec_registry, "register_member", _boom)

        from celine.onboarding.services.audit_service import Actor

        with pytest.raises(EnablementError):
            await review.transition(
                db, submission, SubmissionStatus.APPROVED, actor=Actor.system("test")
            )

        assert submission.status == SubmissionStatus.UNDER_REVIEW
        assert [r["action"] for r in recorded] == ["transition_failed"]
        assert "rec_registry_member" in recorded[0]["detail"]

    async def test_a_successful_approval_records_the_transition(self, db, review_env):
        review, recorded = review_env
        submission = FakeSubmission(status=SubmissionStatus.UNDER_REVIEW, verification=OFFLINE)

        from celine.onboarding.services.audit_service import Actor

        await review.transition(
            db, submission, SubmissionStatus.APPROVED, actor=Actor.system("test")
        )

        assert submission.status == SubmissionStatus.APPROVED
        assert [r["action"] for r in recorded] == ["transition"]
        assert recorded[0]["detail"] == "under_review -> approved"

    async def test_a_rejection_reason_reaches_the_trail(self, db, review_env):
        review, recorded = review_env
        submission = FakeSubmission(status=SubmissionStatus.UNDER_REVIEW)

        from celine.onboarding.services.audit_service import Actor

        await review.transition(
            db,
            submission,
            SubmissionStatus.REJECTED,
            actor=Actor.system("test"),
            reason="POD belongs to another supply",
        )
        assert "POD belongs to another supply" in recorded[0]["detail"]


# ---------------------------------------------------------------------------
# Uploaded documents
# ---------------------------------------------------------------------------


class TestUploadedDocumentsAreDiscardedOnActivation:
    """A bill or an identity document is kept until the account is active, not
    after: the check survives in the credential's `verificationMethod`."""

    async def test_discarded_once_approval_completes(self, db, submission, happy_path, discarded):
        await enablement.enable(db, submission)
        assert discarded == [submission.ref]

    async def test_kept_while_a_fail_closed_step_has_failed(
        self, db, submission, monkeypatch, happy_path, discarded
    ):
        """The operator may still need the copy to decide what went wrong."""

        async def _identity(sub, **kwargs):
            raise RuntimeError("identity registry unreachable")

        monkeypatch.setattr(dataspace_identity, "provision_user_identity", _identity)

        with pytest.raises(EnablementError):
            await enablement.enable(db, submission)

        assert discarded == []
