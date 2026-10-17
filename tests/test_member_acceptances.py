"""The member's own acceptances, `/api/me/acceptances`.

The web app's terms gate reads this on a miss, under the member's token, and does not ask
again what matches. So what is asserted here is almost entirely **whose** application
answers: a wrong one would silence a question the member was never asked.

The query runs for real, against SQLite, with every table the models declare. The token is
verified for real too (`issue_token`): signature, issuer, audience and expiry.
"""

from __future__ import annotations

import importlib
import pkgutil
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import celine.onboarding.models as models_pkg
from celine.onboarding.models.database import Base, get_db
from celine.onboarding.models.enablement import (
    EnablementStatus,
    EnablementStep,
    SubmissionEnablementStep,
)
from celine.onboarding.models.rec import Rec
from celine.onboarding.models.submission import Submission, SubmissionStatus


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):
    return "JSON"


for _module in pkgutil.iter_modules(models_pkg.__path__):
    importlib.import_module(f"{models_pkg.__name__}.{_module.name}")

MEMBER_UUID = "0b1f7c2e-1111-4a5b-9c3d-member000001"
OTHER_UUID = "0b1f7c2e-2222-4a5b-9c3d-member000002"
SHA_IT = "a" * 64


class _AsyncShim:
    """An `AsyncSession` as far as the route uses one, over a synchronous session."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.session.execute(statement)


@pytest.fixture()
def db():
    # One connection for every thread: the TestClient runs the app in another one.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        for slug in ("rec-a", "rec-b"):
            session.add(Rec(slug=slug, name=slug, manifest={}))
        session.commit()
        yield session


@pytest.fixture()
def shim(db):
    return _AsyncShim(db)


@pytest.fixture()
def client(issue_token, seed_rec, shim):
    """The real app, two communities, and the database above."""
    from celine.onboarding.main import create_app

    seed_rec("rec-a", organization="rec-a")
    seed_rec("rec-b", organization="rec-b")
    app = create_app()

    async def _db():
        yield shim

    app.dependency_overrides[get_db] = _db
    return TestClient(app, raise_server_exceptions=False)


def _application(
    db: Session,
    *,
    rec_slug: str = "rec-a",
    status: SubmissionStatus = SubmissionStatus.APPROVED,
    login: str | None = MEMBER_UUID,
    step: EnablementStep = EnablementStep.KEYCLOAK_USER,
    step_status: EnablementStatus = EnablementStatus.SUCCEEDED,
    created_at: datetime | None = None,
    **columns,
) -> Submission:
    """An application whose login step recorded *login* as the Keycloak uuid."""
    at = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
    consents = {
        "gdpr_consent": True,
        "gdpr_consent_at": at,
        "gdpr_consent_version": "2024-01-01",
        "gdpr_consent_url": "https://www.example.org/privacy/",
        "terms_consent": True,
        "terms_consent_at": at,
        "terms_consent_version": "0.2",
        "terms_consent_url": "https://legal.example.org/example-rec/terms-of-use/0.2/it/",
        "terms_consent_sha256": SHA_IT,
        "terms_clauses_consent": True,
        "terms_clauses_consent_at": at,
        "terms_clauses_consent_version": "0.2",
        "terms_clauses_consent_url": "https://legal.example.org/example-rec/terms-of-use/0.2/it/",
        "terms_clauses_consent_sha256": SHA_IT,
        **columns,
    }
    submission = Submission(
        rec_slug=rec_slug,
        consent_ip="192.0.2.1",
        status=status,
        email="member@example.org",
        first_name="Ada",
        fiscal_code="RSSMRA80A01H501U",
        **consents,
    )
    if created_at is not None:
        submission.created_at = created_at
    db.add(submission)
    db.flush()
    db.add(
        SubmissionEnablementStep(
            submission_id=submission.id,
            step=step.value,
            status=step_status.value,
            external_ref=login,
        )
    )
    db.commit()
    return submission


def _member_token(
    issue_token, *, sub: str = MEMBER_UUID, organization: str | None = "rec-a", **claims
):
    if organization is not None:
        claims["organization"] = {organization: {"id": "org-uuid", "groups": []}}
    return issue_token(
        sub=sub,
        email=claims.pop("email", "member@example.org"),
        preferred_username=claims.pop("preferred_username", "member@example.org"),
        **claims,
    )


def _get(client, token: str | None, path: str = "/api/me/acceptances"):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get(path, headers=headers)


class TestTheTokenIsVerified:
    """No capability on this surface, so the token is the whole of the authentication."""

    def test_no_token_is_refused(self, client, shim):
        """
        @verifies REQ-0050
        """
        assert _get(client, None).status_code == 401
        assert shim.statements == []

    def test_a_token_signed_by_another_key_is_refused(self, client, shim, issue_token):
        """
        @verifies REQ-0050
        """
        import jwt as pyjwt
        from cryptography.hazmat.primitives.asymmetric import rsa

        good = pyjwt.decode(_member_token(issue_token), options={"verify_signature": False})
        forged = pyjwt.encode(
            good, rsa.generate_private_key(public_exponent=65537, key_size=2048), algorithm="RS256"
        )

        assert _get(client, forged).status_code == 401
        assert shim.statements == []

    def test_an_unsigned_token_is_refused(self, client, shim, issue_token):
        """
        @verifies REQ-0050
        """
        import jwt as pyjwt

        good = pyjwt.decode(_member_token(issue_token), options={"verify_signature": False})
        unsigned = pyjwt.encode(good, None, algorithm="none")

        assert _get(client, unsigned).status_code == 401
        assert shim.statements == []

    def test_an_expired_token_is_refused(self, client, shim, issue_token):
        """
        @verifies REQ-0050
        """
        token = _member_token(issue_token, exp=int(time.time()) - 600, iat=int(time.time()) - 900)

        assert _get(client, token).status_code == 401
        assert shim.statements == []

    def test_a_token_for_another_audience_is_refused(self, client, shim, issue_token):
        """
        @verifies REQ-0050
        """
        assert _get(client, _member_token(issue_token, aud="svc-elsewhere")).status_code == 401
        assert shim.statements == []

    def test_a_token_from_another_issuer_is_refused(self, client, shim, issue_token):
        """
        @verifies REQ-0050
        """
        token = _member_token(issue_token, iss="https://keycloak.test/realms/elsewhere")

        assert _get(client, token).status_code == 401
        assert shim.statements == []

    def test_a_token_in_the_query_string_is_not_read(self, client, shim, issue_token):
        """
        @verifies REQ-0050
        """
        token = _member_token(issue_token)

        assert client.get(f"/api/me/acceptances?access_token={token}").status_code == 401


class TestOnlyAMembersOwnToken:
    def test_a_service_account_is_refused(self, client, shim, db, service_token):
        """Even holding `onboarding.admin`: a client acting for a member forwards their token.

        @verifies REQ-0050
        """
        _application(db)

        resp = _get(client, service_token("onboarding.admin"))

        assert resp.status_code == 403
        assert shim.statements == []

    def test_a_service_account_named_like_a_members_uuid_is_refused(
        self, client, shim, db, issue_token
    ):
        """A client whose `sub` is a member's uuid is still a client.

        @verifies REQ-0050
        """
        _application(db)
        token = issue_token(
            sub=MEMBER_UUID,
            preferred_username="service-account-svc-elsewhere",
            client_id="svc-elsewhere",
            azp="svc-elsewhere",
            organization={"rec-a": {"id": "org-uuid", "groups": []}},
        )

        assert _get(client, token).status_code == 403
        assert shim.statements == []

    def test_no_capability_is_needed_and_none_is_consulted(
        self, client, db, issue_token, monkeypatch
    ):
        """A member holds no console group; the operator policy must not be asked.

        @verifies REQ-0050
        """
        from celine.onboarding.security import policy

        def _never(*args, **kwargs):
            raise AssertionError("the member surface consulted the operator policy")

        monkeypatch.setattr(policy, "get_policy", _never)
        monkeypatch.setattr(policy.OnboardingAccessPolicy, "__init__", _never)
        _application(db)

        resp = _get(client, _member_token(issue_token))

        assert resp.status_code == 200
        assert resp.json()["submission_id"] is not None


class TestTheirOwnApplicationAndNoOneElses:
    def test_the_members_approved_application_answers(self, client, db, issue_token):
        """
        @verifies REQ-0049
        """
        mine = _application(db)

        body = _get(client, _member_token(issue_token)).json()

        assert body["submission_id"] == str(mine.id)
        assert body["rec_slug"] == "rec-a"
        by_slot = {a["slot"]: a for a in body["acceptances"]}
        assert set(by_slot) == {"gdpr", "terms", "terms_clauses"}
        assert by_slot["gdpr"]["legal_slot"] == "privacy"
        assert by_slot["terms"] == {
            "slot": "terms",
            "legal_slot": "terms",
            "version": "0.2",
            "url": "https://legal.example.org/example-rec/terms-of-use/0.2/it/",
            "sha256": SHA_IT,
            "accepted_at": "2026-10-17T09:00:00Z",
        }
        assert by_slot["terms_clauses"]["legal_slot"] == "terms_clauses"

    def test_another_members_application_is_not_returned(self, client, db, issue_token):
        """
        @verifies REQ-0049
        """
        _application(db, login=OTHER_UUID)

        body = _get(client, _member_token(issue_token)).json()

        assert body == {"submission_id": None, "rec_slug": "rec-a", "acceptances": []}

    def test_the_email_and_the_username_find_nothing(self, client, db, issue_token):
        """Same email and username as the application, another account: not theirs.

        An adopted account's username is not its email, and an email names whoever typed
        it into the form. Only the uuid the login step recorded is evidence.
        @verifies REQ-0049
        """
        _application(db, login=OTHER_UUID)
        token = _member_token(
            issue_token, email="member@example.org", preferred_username="member@example.org"
        )

        assert _get(client, token).json()["submission_id"] is None

    def test_the_query_binds_the_tokens_sub_and_nothing_the_caller_sent(
        self, client, shim, db, issue_token
    ):
        """
        @verifies REQ-0050
        """
        _application(db)

        _get(client, _member_token(issue_token), "/api/me/acceptances?sub=x&rec_slug=rec-b")

        (statement,) = shim.statements
        bound = set(statement.compile().params.values())
        assert MEMBER_UUID in bound
        assert "rec-a" in bound
        assert not bound & {"x", "rec-b", "member@example.org"}

    def test_query_parameters_cannot_point_it_elsewhere(self, client, db, issue_token):
        """
        @verifies REQ-0050
        """
        mine = _application(db)
        other = _application(db, login=OTHER_UUID)
        _application(db, rec_slug="rec-b")

        resp = _get(
            client,
            _member_token(issue_token),
            f"/api/me/acceptances?submission_id={other.id}&rec_slug=rec-b&sub={OTHER_UUID}",
        )

        assert resp.json()["submission_id"] == str(mine.id)

    def test_a_path_segment_does_not_reach_another_application(self, client, db, issue_token):
        """
        @verifies REQ-0050
        """
        other = _application(db, login=OTHER_UUID)

        resp = _get(client, _member_token(issue_token), f"/api/me/acceptances/{other.id}")

        assert resp.status_code in (404, 405)
        assert str(other.id) not in resp.text

    def test_the_community_is_the_tokens(self, client, db, issue_token):
        """Released from rec-b and now in rec-a, one Keycloak account: rec-b's is not shown.

        The uuid alone would name the application in the community they left.
        @verifies REQ-0049
        """
        _application(db, rec_slug="rec-b")

        body = _get(client, _member_token(issue_token, organization="rec-a")).json()

        assert body == {"submission_id": None, "rec_slug": "rec-a", "acceptances": []}

    def test_a_token_naming_no_community_served_here_finds_nothing(
        self, client, shim, db, issue_token
    ):
        """
        @verifies REQ-0049
        """
        _application(db)

        for organization in (None, "rec-unknown"):
            body = _get(client, _member_token(issue_token, organization=organization)).json()
            assert body == {"submission_id": None, "rec_slug": None, "acceptances": []}
        assert shim.statements == []

    def test_a_member_of_two_communities_is_refused(self, client, shim, db, issue_token):
        """Picking one would answer for a community the reader did not mean.

        @verifies REQ-0050
        """
        _application(db)
        token = issue_token(
            sub=MEMBER_UUID,
            email="member@example.org",
            organization={
                "rec-a": {"id": "org-a", "groups": []},
                "rec-b": {"id": "org-b", "groups": []},
            },
        )

        resp = _get(client, token)

        assert resp.status_code == 409
        assert shim.statements == []


class TestOnlyAnEnabledApplication:
    @pytest.mark.parametrize(
        "status",
        [
            SubmissionStatus.DRAFT,
            SubmissionStatus.SUBMITTED,
            SubmissionStatus.UNDER_REVIEW,
            SubmissionStatus.REJECTED,
        ],
    )
    def test_an_application_not_approved_does_not_answer(self, client, db, issue_token, status):
        """
        @verifies REQ-0049
        """
        _application(db, status=status)

        assert _get(client, _member_token(issue_token)).json()["submission_id"] is None

    @pytest.mark.parametrize(
        "step_status",
        [
            EnablementStatus.PENDING,
            EnablementStatus.RUNNING,
            EnablementStatus.FAILED,
            EnablementStatus.SKIPPED,
        ],
    )
    def test_a_login_step_that_did_not_succeed_does_not_answer(
        self, client, db, issue_token, step_status
    ):
        """
        @verifies REQ-0049
        """
        _application(db, step_status=step_status)

        assert _get(client, _member_token(issue_token)).json()["submission_id"] is None

    def test_a_revoked_login_does_not_answer(self, client, db, issue_token):
        """`enablement.revoke` leaves the row pending and clears its `external_ref`.

        @verifies REQ-0049
        """
        _application(db, step_status=EnablementStatus.PENDING, login=None)

        assert _get(client, _member_token(issue_token)).json()["submission_id"] is None

    @pytest.mark.parametrize(
        "step", [s for s in EnablementStep if s is not EnablementStep.KEYCLOAK_USER]
    )
    def test_another_steps_reference_is_not_a_login(self, client, db, issue_token, step):
        """Only the login step's `external_ref` is a Keycloak uuid.

        @verifies REQ-0049
        """
        _application(db, step=step)

        assert _get(client, _member_token(issue_token)).json()["submission_id"] is None

    def test_the_latest_approved_application_answers(self, client, db, issue_token):
        """
        @verifies REQ-0049
        """
        _application(db, created_at=datetime(2026, 1, 1, tzinfo=UTC), terms_consent_version="0.1")
        latest = _application(db, created_at=datetime(2026, 10, 1, tzinfo=UTC))

        body = _get(client, _member_token(issue_token)).json()

        assert body["submission_id"] == str(latest.id)
        assert {a["version"] for a in body["acceptances"] if a["slot"] == "terms"} == {"0.2"}


class TestWhatIsReturned:
    def test_nothing_but_the_acceptances(self, client, db, issue_token):
        """No personal data of the application, and no evidence of how it was made.

        @verifies REQ-0049
        """
        _application(db)

        resp = _get(client, _member_token(issue_token))

        assert set(resp.json()) == {"submission_id", "rec_slug", "acceptances"}
        for leaked in ("member@example.org", "Ada", "RSSMRA80A01H501U", "192.0.2.1", MEMBER_UUID):
            assert leaked not in resp.text
        for acceptance in resp.json()["acceptances"]:
            assert set(acceptance) == {
                "slot",
                "legal_slot",
                "version",
                "url",
                "sha256",
                "accepted_at",
            }

    def test_a_slot_not_accepted_or_without_a_version_is_left_out(self, client, db, issue_token):
        """Without a version there is nothing to match, so the reader asks.

        @verifies REQ-0049
        """
        _application(
            db,
            gdpr_consent_version=None,
            terms_clauses_consent=False,
            terms_clauses_consent_at=None,
        )

        body = _get(client, _member_token(issue_token)).json()

        assert {a["slot"] for a in body["acceptances"]} == {"terms"}

    def test_the_clauses_are_never_implied_by_the_terms(self, client, db, issue_token):
        """
        @verifies REQ-0049
        """
        _application(db, terms_clauses_consent=False)

        slots = {a["slot"] for a in _get(client, _member_token(issue_token)).json()["acceptances"]}

        assert "terms" in slots
        assert "terms_clauses" not in slots


def test_the_lookup_is_indexed():
    """The login step's uuid is what the member's every miss looks up."""
    index = {
        i.name: [c.name for c in i.columns] for i in SubmissionEnablementStep.__table__.indexes
    }

    assert index["ix_enablement_step_external_ref"] == ["step", "external_ref"]


class TestTheMigration:
    def test_upgrade_adds_what_the_model_declares(self, monkeypatch):
        from test_sharing_intent import _offline_sql

        sql = _offline_sql(monkeypatch, "0021:0022")

        model = {c.name for c in Submission.__table__.columns}
        for slot in ("terms", "terms_clauses"):
            for suffix in ("", "_at", "_version", "_url", "_sha256"):
                column = f"{slot}_consent{suffix}"
                assert column in model
                assert f"ALTER TABLE submissions ADD COLUMN {column} " in sql
        assert "ADD COLUMN terms_consent BOOLEAN DEFAULT false NOT NULL" in sql
        assert (
            "CREATE INDEX ix_enablement_step_external_ref "
            "ON submission_enablement_steps (step, external_ref)"
        ) in sql

    def test_downgrade_drops_them(self, monkeypatch):
        from test_sharing_intent import _offline_sql

        sql = _offline_sql(monkeypatch, "0022:0021", downgrade=True)

        assert "DROP INDEX ix_enablement_step_external_ref" in sql
        assert "DROP COLUMN terms_clauses_consent_sha256" in sql
        assert "DROP COLUMN terms_consent;" in sql
