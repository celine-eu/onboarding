"""`onboarding-cli admin` against a live app.

`tests/test_cli_admin.py` proves the CLI calls the right endpoints with the right
payloads, against a mocked network. This proves the other half: that those calls
are actually accepted, authorized and recorded by a running service — that the
console and the terminal reach the same state machine rather than two.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest

from .conftest import ORG, REC

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def cli(api: str, idp):
    """Run `onboarding-cli admin …` against the live app as a real operator."""

    def _run(*args: str, group: str = "admins", expect: int | None = 0):
        token = idp.operator(ORG, group)
        result = subprocess.run(
            [
                "uv",
                "run",
                "--project",
                "src",
                "onboarding-cli",
                "admin",
                *args,
                "--api-url",
                api,
                "--token",
                token,
            ],
            cwd=REPO_ROOT,
            env={**os.environ, "DATABASE_URL": os.environ["ONBOARDING_E2E_DATABASE_URL"]},
            capture_output=True,
            text=True,
        )
        if expect is not None:
            assert result.returncode == expect, (
                f"exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    return _run


class TestIdentity:
    def test_whoami_reports_the_operator_and_their_communities(self, cli):
        payload = json.loads(cli("whoami", "--json").stdout)
        assert payload["subject_type"] == "user"
        assert payload["email"] == "operator@example.org"
        assert [r["slug"] for r in payload["recs"]] == [REC]

    def test_capabilities_follow_the_group(self, cli):
        viewer = json.loads(cli("whoami", "--json", group="viewers").stdout)
        capabilities = viewer["recs"][0]["capabilities"]
        assert "submissions.read" in capabilities
        assert "submissions.review" not in capabilities


class TestReview:
    def test_the_queue_matches_the_api(self, cli, client: httpx.Client, idp, submission):
        made = submission(REC)
        payload = json.loads(cli("review", "list", "--rec", REC, "--json").stdout)
        assert made["ref"] in [s["ref"] for s in payload["submissions"]]

    def test_a_submission_is_addressed_by_reference(self, cli, submission):
        made = submission(REC)
        payload = json.loads(cli("review", "show", made["ref"], "--rec", REC, "--json").stdout)
        assert payload["id"] == made["id"]

    def test_identifiers_are_masked_unless_revealed(self, cli, submission):
        made = submission(REC)
        masked = json.loads(cli("review", "show", made["ref"], "--rec", REC, "--json").stdout)
        assert "•" in masked["fiscal_code"]

        revealed = json.loads(
            cli("review", "show", made["ref"], "--rec", REC, "--reveal", "--json").stdout
        )
        assert revealed["fiscal_code"] == "RSSMRA85T10A562S"

    def test_a_viewer_is_refused_by_the_server(self, cli, submission):
        """The CLI does not decide; it reports what the policy said."""
        made = submission(REC)
        result = cli("review", "approve", made["ref"], "--rec", REC, group="viewers", expect=1)
        assert "Not permitted" in result.stdout + result.stderr

    def test_approve_moves_the_submission_and_the_api_agrees(
        self, cli, client: httpx.Client, idp, submission
    ):
        made = submission(REC)
        cli("review", "approve", made["ref"], "--rec", REC)

        headers = {"Authorization": f"Bearer {idp.operator(ORG, 'viewers')}"}
        current = client.get(f"/api/admin/{REC}/submissions/{made['id']}", headers=headers).json()
        assert current["status"] == "approved"

    def test_reject_without_a_reason_never_reaches_the_api(self, cli, submission):
        made = submission(REC)
        result = cli("review", "reject", made["ref"], "--rec", REC, expect=1)
        assert "--reason is required" in result.stdout + result.stderr

    def test_reject_records_the_reason_in_the_trail(
        self, cli, client: httpx.Client, idp, submission
    ):
        made = submission(REC)
        cli("review", "reject", made["ref"], "--rec", REC, "--reason", "POD non valido")

        headers = {"Authorization": f"Bearer {idp.operator(ORG, 'viewers')}"}
        trail = client.get(f"/api/admin/{REC}/audit-logs", headers=headers).json()
        entry = next(
            e for e in trail if e["entity_id"] == made["id"] and e["action"] == "transition"
        )
        assert "POD non valido" in entry["detail"]

    def test_an_ambiguous_reference_is_refused(self, cli, submission):
        submission(REC)
        submission(REC)
        result = cli("review", "show", "2026", "--rec", REC, expect=1)
        assert "Use the full reference" in result.stdout + result.stderr


class TestEnablement:
    def test_status_shows_the_whole_pipeline(self, cli, submission):
        made = submission(REC)
        cli("review", "approve", made["ref"], "--rec", REC)
        payload = json.loads(
            cli("enablement", "status", made["ref"], "--rec", REC, "--json").stdout
        )
        assert [s["step"] for s in payload["steps"]] == [
            "keycloak_user",
            "rec_registry_member",
            "dataspace_identity",
            "dataspace_share",
        ]
        assert payload["state"] == "complete"

    def test_retry_exits_zero_when_the_pipeline_is_healthy(self, cli, submission):
        made = submission(REC)
        cli("review", "approve", made["ref"], "--rec", REC)
        cli("enablement", "retry", made["ref"], "--rec", REC)

    def test_an_unknown_step_is_refused(self, cli, submission):
        made = submission(REC)
        cli("review", "approve", made["ref"], "--rec", REC)
        result = cli(
            "enablement", "retry", made["ref"], "--rec", REC, "--step", "teleport", expect=1
        )
        assert "Unknown enablement step" in result.stdout + result.stderr

    def test_revoke_needs_confirmation(self, cli, submission):
        made = submission(REC)
        result = cli("enablement", "revoke", made["ref"], "--rec", REC, expect=1)
        assert "--confirm" in result.stdout + result.stderr


class TestAuditAndPurge:
    def test_the_cli_is_recorded_as_the_operator_it_authenticated_as(self, cli, submission):
        """Not as "the CLI" — the token names a person, and so does the trail."""
        made = submission(REC)
        cli("review", "approve", made["ref"], "--rec", REC)
        entries = json.loads(cli("audit", "--rec", REC, "--json").stdout)
        entry = next(
            e for e in entries if e["entity_id"] == made["id"] and e["action"] == "transition"
        )
        assert entry["actor_type"] == "user"
        assert entry["actor_email"] == "operator@example.org"

    def test_purge_needs_confirmation(self, cli, submission):
        made = submission(REC)
        result = cli("purge", made["ref"], "--rec", REC, expect=1)
        assert "--confirm" in result.stdout + result.stderr

    def test_purge_removes_the_submission(self, cli, client: httpx.Client, idp, submission):
        made = submission(REC)
        cli("purge", made["ref"], "--rec", REC, "--confirm")

        headers = {"Authorization": f"Bearer {idp.operator(ORG, 'viewers')}"}
        gone = client.get(f"/api/admin/{REC}/submissions/{made['id']}", headers=headers)
        assert gone.status_code == 404
