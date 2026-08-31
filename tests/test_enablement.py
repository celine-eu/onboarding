"""What approval does, and what happens when part of it does not.

The pipeline was previously three fire-and-forget calls: a failure was a 422 whose
only remedy was pressing Approve again and re-running everything. These tests pin
the behaviour that replaced it — per-step state, a fail-closed boundary, and a
retry that finishes the job rather than restarting it.
"""

from __future__ import annotations

import uuid

import pytest

from celine.onboarding.models.enablement import (
    EnablementStatus,
    EnablementStep,
    SubmissionEnablementStep,
)
from celine.onboarding.models.submission import SubmissionStatus
from celine.onboarding.services import (
    dataspace_identity,
    enablement,
    keycloak_identity,
    rec_registry,
)
from celine.onboarding.services.enablement import EnablementError
from celine.onboarding.services.keycloak_identity import KeycloakProvisionResult


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
        self.__dict__.update(kwargs)


@pytest.fixture()
def db() -> FakeDb:
    return FakeDb()


@pytest.fixture()
def submission() -> FakeSubmission:
    return FakeSubmission()


@pytest.fixture()
def happy_path(monkeypatch):
    """Every step succeeds, and records what the runner should carry forward."""
    calls: list[str] = []

    async def _kc(sub):
        calls.append("keycloak_user")
        return KeycloakProvisionResult(user_id="kc-123", username=sub.email, created=True)

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

    monkeypatch.setattr(keycloak_identity, "provision_keycloak_user", _kc)
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

    async def test_the_username_is_the_one_keycloak_reported(
        self, db, submission, happy_path, monkeypatch
    ):
        """A user provisioning found rather than created may log in under a name
        that is not their email, and it is that value the registry needs — not
        the email we asked by."""

        async def _kc(sub):
            happy_path.append("keycloak_user")
            return KeycloakProvisionResult(user_id="kc-123", username="gl-00001", created=False)

        monkeypatch.setattr(keycloak_identity, "provision_keycloak_user", _kc)

        await enablement.enable(db, submission)

        assert "rec_registry_member(user=gl-00001)" in happy_path

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


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


class TestRevoke:
    @pytest.fixture()
    def revocations(self, monkeypatch):
        done: list[str] = []

        async def _disable_kc(user_id):
            done.append(f"keycloak_user:{user_id}")

        async def _deactivate(sub, *, member_key):
            done.append(f"rec_registry_member:{member_key}")
            return "deactivated"

        async def _revoke_identity(sub):
            done.append("dataspace_identity")
            sub.dataspace_vc_id = None
            return "revoked"

        monkeypatch.setattr(keycloak_identity, "disable_keycloak_user", _disable_kc)
        monkeypatch.setattr(rec_registry, "deactivate_member", _deactivate)
        monkeypatch.setattr(dataspace_identity, "revoke_user_identity", _revoke_identity)
        return done

    async def test_runs_in_reverse_order(self, db, submission, happy_path, revocations):
        await enablement.enable(db, submission)
        await enablement.revoke(db, submission)

        assert [d.split(":")[0] for d in revocations] == [
            "dataspace_identity",
            "rec_registry_member",
            "keycloak_user",
        ]

    async def test_passes_the_recorded_references(self, db, submission, happy_path, revocations):
        await enablement.enable(db, submission)
        await enablement.revoke(db, submission)
        assert "keycloak_user:kc-123" in revocations
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

        async def _withdraw(sub, *, reason="", raise_on_error=False):
            called.append(sub.ref)
            return True

        monkeypatch.setattr(dataspace_identity, "withdraw_user_shares", _withdraw)

        await enablement.enable(db, submission)
        rows = await enablement.revoke(db, submission)

        assert called == [submission.ref]
        # A revoked step goes back to PENDING — the same state the other steps
        # land in, and what `test_revoked_steps_become_retriable_again` relies on.
        assert rows[EnablementStep.DATASPACE_SHARE].status == EnablementStatus.PENDING
        assert rows[EnablementStep.DATASPACE_SHARE].detail == "standing consent withdrawn"

    async def test_the_share_is_withdrawn_before_the_identity_that_carried_it(
        self, db, submission, happy_path, revocations, monkeypatch
    ):
        """Ordering, and it is free: `revoke` walks `reversed(PIPELINE)`.

        Asserted rather than assumed, because the two steps are adjacent and a
        later reordering of the pipeline would silently swap them — leaving the
        withdrawal to be attempted for a DID whose credential is already gone.
        """
        order: list[str] = []

        async def _withdraw(sub, *, reason="", raise_on_error=False):
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
        # ...and the Keycloak user, later in the reverse order, was still disabled.
        assert "keycloak_user:kc-123" in revocations

    async def test_nothing_to_revoke_is_not_an_error(self, db, submission, revocations):
        rows = await enablement.revoke(db, submission)
        assert enablement.state_of(rows) == "not_started"
        assert revocations == []


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
        submission = FakeSubmission(status=SubmissionStatus.UNDER_REVIEW)

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
        submission = FakeSubmission(status=SubmissionStatus.UNDER_REVIEW)

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
