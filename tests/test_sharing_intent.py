"""The member's recorded decision per offer, and the retry that ranks it.

The maintainer decided on 2026-09-19 that the member's toggle records what they
decided before it relays anything, and that the operator's retry ranks that
record with what every connector says — newest wins, a tie is a withdrawal.

Two holes are why, and each has a test here that fails without the record:

1. A withdrawal the member makes at connector X between the retry's read and its
   grant *at X*, while the withdrawal failed everywhere else. The retry's grant
   replaces X's row, and no connector shows the withdrawal any more.
2. ds stamps nothing when a withdrawal meets one that already stands. A member
   whose withdrawal failed at the only connector still granting has left no time
   anywhere, and the retry re-drives the older grant.
"""

# ruff: noqa: F811 — the fixtures imported from test_dataspace_shares are requested by name

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from test_dataspace_identity import _patch_httpx
from test_dataspace_shares import (  # noqa: F401 — fixtures, used by name
    HOLDER_URL,
    OWN_URL,
    POD,
    RELAYED_EVIDENCE,
    RELEASE,
    RESEARCH,
    _Connectors,
    _enable_shares,
    _org_client,
    _research,
    _reset_token_provider,
    held_twice,
)

import celine.onboarding.services.dataspace_identity as di
import celine.onboarding.services.member_sharing as ms
from celine.onboarding.services import template_service

ROOT = Path(__file__).resolve().parents[1]

RESEARCH_OFFER = {
    "id": RESEARCH,
    "requires_consent": True,
    "consent_text_version": "2.0",
    "purpose": "research",
}


class _MemberAndConnectors(_Connectors):
    """The two fake connectors, plus the member's own route at their community.

    `POST /consent/my/shares` is the member deciding as themselves at their own
    connector: recorded as ``subject``, with ds's rules for time (a withdrawal
    over a withdrawal stamps nothing).
    """

    #: base URL → status `/consent/my/shares` answers instead of recording
    member_refuse: dict[str, int]

    def handler(self, req: httpx.Request) -> httpx.Response:
        import json

        url = str(req.url)
        if req.method == "POST" and url.endswith("/consent/my/shares"):
            self.requests.append(req)
            if OWN_URL in getattr(self, "member_refuse", {}):
                return httpx.Response(self.member_refuse[OWN_URL], text="refused")
            body = json.loads(req.read().decode())
            row = self.rows.get((OWN_URL, body["offer_id"]))
            if not (body["enabled"] and row is not None and row["status"] == "granted"):
                self.decide(OWN_URL, body["offer_id"], granted=body["enabled"])
            return httpx.Response(200, json={"ok": True})
        return super().handler(req)


@pytest.fixture()
def member(monkeypatch, submission, held_twice):
    """The same member, able to press their toggle, against the same connectors."""
    fake = _MemberAndConnectors()
    fake.member_refuse = {}
    _patch_httpx(monkeypatch, fake.handler)

    credential = di.SubjectCredential(subject_id=submission.dataspace_did, vc_jws="vc")

    async def _resolve(user, *, provision=True):
        return ms.SharingState.OK, "default", credential

    async def _offer(rec_slug, offer_id):
        return dict(RESEARCH_OFFER, id=offer_id)

    async def _view(user):
        return ms.SharingView(state=ms.SharingState.OK)

    monkeypatch.setattr(ms, "_resolve", _resolve)
    monkeypatch.setattr(template_service, "get_sharing_offer", _offer)
    monkeypatch.setattr(ms, "get_data_sharing", _view)
    monkeypatch.setattr(ms.settings, "ds_connector_url", OWN_URL)
    return fake


async def _press(enabled: bool, offer: str = RESEARCH):
    return await ms.set_data_sharing(object(), offer, enabled=enabled)


def _on_the_connectors_clock(intents, fake) -> None:
    """Date the member's presses on the fake connectors' clock, one tick each."""
    intents.now = lambda: datetime.fromisoformat(fake.tick())


# ── the two holes ─────────────────────────────────────────────────────────────


class TestTheHolesTheRecordCloses:
    async def test_a_withdrawal_a_retry_overwrote_is_still_the_members_newest(
        self, submission, held_twice, intents
    ):
        """Hole 1. The retry read, the member withdrew, the retry wrote.

        The holder refused at approval, so the retry has the holder's half to
        write. Between its read and that write the member withdraws: it lands at
        the holder, and fails at their community. The retry's grant then replaces
        the holder's row, and every connector reads granted.
        """
        held_twice.refuse[HOLDER_URL] = 502
        assert await di.provision_user_shares(submission) is False
        held_twice.refuse.clear()
        _on_the_connectors_clock(intents, held_twice)

        def _member_withdraws_and_only_the_holder_takes_it():
            intents.put(submission.dataspace_did, RESEARCH, granted=False)
            held_twice.decide(HOLDER_URL, RESEARCH, granted=False)

        held_twice.before_first_post = _member_withdraws_and_only_the_holder_takes_it

        assert await di.provision_user_shares(submission, raise_on_error=True) is True

        assert _research(held_twice) == {OWN_URL: "revoked", HOLDER_URL: "revoked"}
        for where in (OWN_URL, HOLDER_URL):
            assert held_twice.rows[(where, RESEARCH)]["decided_by"] == "subject"
        # Nothing else moved: the release was never withdrawn.
        assert held_twice.rows[(HOLDER_URL, RELEASE)]["status"] == "granted"

    async def test_a_withdrawal_no_connector_stamped_is_still_the_members_newest(
        self, submission, member, intents
    ):
        """Hole 2, through the member's own toggle.

        Research is split: the community's half was withdrawn earlier, and the
        member's later grant landed only at the holder. The member now presses
        withdraw: at their community it meets a withdrawal that stands, which ds
        does not re-stamp; the holder, the only connector still granting, refuses.
        Every connector's newest row is then the grant.
        """
        assert await di.provision_user_shares(submission) is True
        member.decide(OWN_URL, RESEARCH, granted=False)
        member.decide(HOLDER_URL, RESEARCH, granted=False)
        member.decide(
            HOLDER_URL,
            RESEARCH,
            granted=True,
            keys=[f"pod:{POD}"],
            legal_basis=dict(RELAYED_EVIDENCE),
        )
        own_before = dict(member.rows[(OWN_URL, RESEARCH)])
        _on_the_connectors_clock(intents, member)
        member.refuse[HOLDER_URL] = 502

        with pytest.raises(ms.SharingUnavailableError):
            await _press(enabled=False)

        # ds recorded nothing new at the community, and the holder refused.
        assert member.rows[(OWN_URL, RESEARCH)] == own_before
        assert member.rows[(HOLDER_URL, RESEARCH)]["status"] == "granted"

        member.refuse.clear()
        assert await di.provision_user_shares(submission, raise_on_error=True) is True

        assert _research(member) == {OWN_URL: "revoked", HOLDER_URL: "revoked"}
        withdrawal = [
            (where, body) for where, body in member.posts() if body.get("enabled") is False
        ][-1]
        assert withdrawal[0] == HOLDER_URL
        assert withdrawal[1]["decided_by"] == "subject"


# ── the toggle records before it relays ──────────────────────────────────────


class TestTheToggleRecordsFirst:
    async def test_the_decision_is_recorded_before_any_connector_is_called(
        self, submission, member, intents
    ):
        seen = len(member.requests)
        calls_when_recorded: list[int] = []
        intents.on_write = lambda intent: calls_when_recorded.append(len(member.requests) - seen)

        await _press(enabled=False)

        assert calls_when_recorded == [0]
        relayed = {where for where, _ in member.posts()} | {
            str(r.url).split("/consent/", 1)[0]
            for r in member.requests[seen:]
            if str(r.url).endswith("/consent/my/shares")
        }
        assert relayed == {OWN_URL, HOLDER_URL}
        assert intents.writes == [(submission.dataspace_did, RESEARCH, False)]

    @pytest.mark.parametrize("refusing", [[HOLDER_URL], [OWN_URL], [OWN_URL, HOLDER_URL]])
    async def test_the_record_survives_a_relay_that_fails(
        self, submission, member, intents, refusing
    ):
        for where in refusing:
            if where == OWN_URL:
                member.member_refuse[OWN_URL] = 502
            else:
                member.refuse[where] = 502

        with pytest.raises(ms.SharingUnavailableError):
            await _press(enabled=False)

        recorded = (await intents.for_subject(submission.dataspace_did))[RESEARCH]
        assert recorded.granted is False
        assert recorded.evidence["source"] == "onboarding-member"
        assert recorded.evidence["consent_text_version"] == "2.0"

    async def test_nothing_is_relayed_when_the_decision_cannot_be_recorded(
        self, submission, member, intents
    ):
        intents.fail_writes = RuntimeError("database is down")
        seen = len(member.requests)

        with pytest.raises(ms.SharingUnavailableError, match="could not be recorded"):
            await _press(enabled=False)

        assert member.requests[seen:] == []
        assert intents.rows == {}

    async def test_pressing_again_replaces_the_decision_and_moves_its_time(
        self, submission, member, intents
    ):
        _on_the_connectors_clock(intents, member)

        await _press(enabled=False)
        first = (await intents.for_subject(submission.dataspace_did))[RESEARCH]
        await _press(enabled=False)
        second = (await intents.for_subject(submission.dataspace_did))[RESEARCH]
        await _press(enabled=True)
        third = (await intents.for_subject(submission.dataspace_did))[RESEARCH]

        assert len(intents.rows) == 1
        assert second.decided_at > first.decided_at
        assert (third.granted, third.decided_at > second.decided_at) == (True, True)


# ── the retry ranks the record ───────────────────────────────────────────────


class TestTheRetryRanksTheRecord:
    async def test_a_record_that_agrees_with_every_connector_writes_nothing_twice(
        self, submission, member, intents
    ):
        assert await di.provision_user_shares(submission) is True
        _on_the_connectors_clock(intents, member)
        await _press(enabled=False)
        converged = {k: dict(v) for k, v in member.rows.items()}
        member.requests.clear()

        assert await di.provision_user_shares(submission, raise_on_error=True) is True
        assert await di.provision_user_shares(submission, raise_on_error=True) is True

        assert member.posts() == []
        assert member.rows == converged
        # The retry reads the record and never writes it.
        assert intents.writes == [(submission.dataspace_did, RESEARCH, False)]

    async def test_an_older_record_does_not_outrank_a_newer_connector_decision(
        self, submission, held_twice, intents
    ):
        assert await di.provision_user_shares(submission) is True
        intents.put(
            submission.dataspace_did,
            RESEARCH,
            granted=False,
            at=datetime(2026, 9, 1, tzinfo=UTC),
        )
        # After it, withdrawn everywhere, then granted again at the community
        # as the member themselves; the holder's half never landed.
        held_twice.decide(HOLDER_URL, RESEARCH, granted=False)
        held_twice.decide(OWN_URL, RESEARCH, granted=False)
        held_twice.decide(OWN_URL, RESEARCH, granted=True, legal_basis=dict(RELAYED_EVIDENCE))

        assert await di.provision_user_shares(submission, raise_on_error=True) is True
        assert _research(held_twice) == {OWN_URL: "granted", HOLDER_URL: "granted"}

    async def test_a_granted_record_is_relayed_with_its_evidence_and_time(
        self, submission, held_twice, intents
    ):
        assert await di.provision_user_shares(submission) is True
        for where in (OWN_URL, HOLDER_URL):
            held_twice.decide(where, RESEARCH, granted=False)
        pressed = datetime.fromisoformat(held_twice.tick())
        intents.put(
            submission.dataspace_did,
            RESEARCH,
            granted=True,
            at=pressed,
            evidence=dict(RELAYED_EVIDENCE),
        )
        held_twice.requests.clear()

        assert await di.provision_user_shares(submission, raise_on_error=True) is True

        assert _research(held_twice) == {OWN_URL: "granted", HOLDER_URL: "granted"}
        posts = dict(held_twice.posts())
        assert posts[HOLDER_URL]["keys"] == [f"pod:{POD}"]
        for body in posts.values():
            assert body["decided_by"] == "subject"
            assert body["legal_basis"]["source"] == "onboarding-member"
            assert body["legal_basis"]["accepted_at"] == pressed.isoformat()

    async def test_a_record_of_an_offer_held_in_one_place_is_carried_there(
        self, submission, held_twice, intents
    ):
        """The release lives at the holder alone; a refused withdrawal left it granted."""
        assert await di.provision_user_shares(submission) is True
        _on_the_connectors_clock(intents, held_twice)
        intents.put(submission.dataspace_did, RELEASE, granted=False)

        assert await di.provision_user_shares(submission, raise_on_error=True) is True
        assert held_twice.rows[(HOLDER_URL, RELEASE)]["status"] == "revoked"

    async def test_a_record_that_cannot_be_read_fails_and_nothing_is_written(
        self, submission, held_twice, intents
    ):
        held_twice.refuse[HOLDER_URL] = 502
        assert await di.provision_user_shares(submission) is False
        held_twice.refuse.clear()
        held_twice.requests.clear()
        intents.fail_reads = RuntimeError("database is down")

        with pytest.raises(ValueError, match="recorded decisions"):
            await di.provision_user_shares(submission, raise_on_error=True)

        assert held_twice.posts() == []


# ── ds's operator override ───────────────────────────────────────────────────


class TestAnOperatorOverride:
    def test_an_override_is_dated_by_its_row_not_by_the_consent_it_carries(self):
        row = {
            "status": "granted",
            "decided_by": "operator",
            "decided_at": "2026-09-19T12:00:00+00:00",
            "legal_basis": {"accepted_at": "2026-07-13T10:00:00+00:00"},
        }
        assert di._decision_time(row) == datetime(2026, 9, 19, 12, tzinfo=UTC)
        relayed = dict(row, decided_by="subject")
        assert di._decision_time(relayed) == datetime(2026, 7, 13, 10, tzinfo=UTC)

    async def test_an_override_newer_than_the_withdrawal_it_lifted_wins(
        self, submission, held_twice, intents
    ):
        """The member withdrew (recorded), then asked the operator to restore it."""
        assert await di.provision_user_shares(submission) is True
        _on_the_connectors_clock(intents, held_twice)
        intents.put(submission.dataspace_did, RESEARCH, granted=False)
        held_twice.decide(OWN_URL, RESEARCH, granted=False)
        held_twice.decide(HOLDER_URL, RESEARCH, granted=False)
        held_twice.decide(
            HOLDER_URL,
            RESEARCH,
            granted=True,
            decided_by="operator",
            keys=[f"pod:{POD}"],
            legal_basis=dict(RELAYED_EVIDENCE, accepted_at="2026-07-13T10:00:00+00:00"),
        )

        assert await di.provision_user_shares(submission, raise_on_error=True) is True
        assert _research(held_twice) == {OWN_URL: "granted", HOLDER_URL: "granted"}

    async def test_a_press_after_the_override_wins_back(self, submission, held_twice, intents):
        assert await di.provision_user_shares(submission) is True
        for where in (OWN_URL, HOLDER_URL):
            held_twice.decide(where, RESEARCH, granted=False)
        held_twice.decide(
            HOLDER_URL, RESEARCH, granted=True, decided_by="operator", keys=[f"pod:{POD}"]
        )
        held_twice.decide(OWN_URL, RESEARCH, granted=True, decided_by="operator")
        _on_the_connectors_clock(intents, held_twice)
        intents.put(submission.dataspace_did, RESEARCH, granted=False)

        assert await di.provision_user_shares(submission, raise_on_error=True) is True
        assert _research(held_twice) == {OWN_URL: "revoked", HOLDER_URL: "revoked"}


# ── a member who declined everything on the form ─────────────────────────────


class TestAMemberWhoDeclinedOnTheForm:
    async def test_a_split_they_made_on_their_page_is_converged(
        self, submission, held_twice, intents
    ):
        submission.data_sharing_consent = False
        submission.data_sharing_consent_offer_ids = []
        # On their page later: research granted; only the community took it.
        held_twice.decide(OWN_URL, RESEARCH, granted=True)
        _on_the_connectors_clock(intents, held_twice)
        intents.put(
            submission.dataspace_did, RESEARCH, granted=True, evidence=dict(RELAYED_EVIDENCE)
        )

        assert await di.provision_user_shares(submission, raise_on_error=True) is True

        assert _research(held_twice) == {OWN_URL: "granted", HOLDER_URL: "granted"}
        # Nothing the form did not accept, and the member did not decide, is written.
        assert {offer for _, offer in held_twice.rows} == {RESEARCH}
        assert submission.share_provisioned is False

    async def test_with_nothing_decided_nothing_is_written(self, submission, held_twice):
        submission.data_sharing_consent = False
        submission.data_sharing_consent_offer_ids = []

        assert await di.provision_user_shares(submission, raise_on_error=True) is True
        assert held_twice.posts() == []


# ── the table ────────────────────────────────────────────────────────────────


def _offline_sql(monkeypatch, revisions: str, *, downgrade: bool = False) -> str:
    """What alembic would run, rendered for PostgreSQL without connecting."""
    from alembic.config import Config

    from alembic import command
    from celine.onboarding.config.settings import settings

    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://u:p@db.invalid/x")
    buffer = io.StringIO()
    # No ini file: `env.py` would hand it to `fileConfig`, which reconfigures the
    # logging every later test in this session reads.
    config = Config(output_buffer=buffer)
    config.set_main_option("script_location", str(ROOT / "alembic"))
    (command.downgrade if downgrade else command.upgrade)(config, revisions, sql=True)
    return buffer.getvalue()


class TestTheMigration:
    def test_upgrade_creates_the_table_the_model_describes(self, monkeypatch):
        from celine.onboarding.models.sharing_intent import MemberSharingIntent

        sql = _offline_sql(monkeypatch, "0013:0014")

        assert "CREATE TABLE member_sharing_intents" in sql
        for column in MemberSharingIntent.__table__.columns:
            assert f"\n    {column.name} " in sql, column.name
        assert (
            "CONSTRAINT uq_member_sharing_intent_subject_offer UNIQUE (subject_id, offer_id)" in sql
        )
        assert "evidence JSONB" in sql
        assert "decided_at TIMESTAMP WITH TIME ZONE NOT NULL" in sql
        assert "UPDATE alembic_version SET version_num='0014'" in sql

    def test_downgrade_drops_it(self, monkeypatch):
        sql = _offline_sql(monkeypatch, "0014:0013", downgrade=True)

        assert "DROP TABLE member_sharing_intents" in sql
        assert "UPDATE alembic_version SET version_num='0013'" in sql

    def test_it_is_the_head(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        config = Config()
        config.set_main_option("script_location", str(ROOT / "alembic"))
        assert ScriptDirectory.from_config(config).get_heads() == ["0014"]


class TestTheWrite:
    def test_one_row_per_member_and_offer_replaced_in_place(self):
        """The upsert is the idempotence: a second press replaces, never appends."""
        from sqlalchemy.dialects import postgresql

        from celine.onboarding.services import sharing_intent

        statement = sharing_intent.upsert_statement(
            subject_id="did:web:users.example:ex-00001",
            rec_slug="default",
            offer_id=RESEARCH,
            granted=False,
            decided_at=datetime(2026, 9, 19, tzinfo=UTC),
            evidence=None,
        )
        sql = str(statement.compile(dialect=postgresql.dialect()))

        assert sql.startswith("INSERT INTO member_sharing_intents")
        assert "ON CONFLICT ON CONSTRAINT uq_member_sharing_intent_subject_offer DO UPDATE" in sql
        for column in ("granted", "decided_at", "evidence", "rec_slug"):
            assert f"{column} = excluded.{column}" in sql, column
        # The key is never rewritten.
        assert "subject_id = excluded" not in sql
        assert "offer_id = excluded" not in sql
