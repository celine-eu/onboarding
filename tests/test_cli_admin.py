"""`onboarding-cli admin`, driven through the real HTTP transport.

The API transport is exercised against a mocked HTTP layer rather than stubbed
out, because "does the CLI call the endpoint the console calls" is the property
worth testing — a CLI with its own idea of the rules is a second implementation
nobody reviews.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from celine.onboarding.cli.main import app

runner = CliRunner()
API = "http://api.test"

SUBMISSION = {
    "id": "11111111-1111-1111-1111-111111111111",
    "ref": "20260730-aaa1",
    "rec_slug": "rec-a",
    "status": "submitted",
    "first_name": "Mario",
    "last_name": "Rossi",
    "email": "mario@example.org",
    "fiscal_code": "••••••••••••562S",
    "pod_code": "••••••••••5678",
}

ENABLEMENT = {
    "submission_id": SUBMISSION["id"],
    "state": "failed",
    "steps": [
        {
            "step": "keycloak_user",
            "label": "Login identity",
            "fail_closed": True,
            "status": "succeeded",
            "external_ref": "kc-1",
            "attempts": 1,
            "last_error": None,
            "detail": "created",
            "started_at": None,
            "completed_at": None,
        },
        {
            "step": "rec_registry_member",
            "label": "Community member",
            "fail_closed": True,
            "status": "failed",
            "external_ref": None,
            "attempts": 2,
            "last_error": "ValueError: registry unreachable",
            "detail": None,
            "started_at": None,
            "completed_at": None,
        },
    ],
}


@pytest.fixture()
def api(monkeypatch):
    """A mocked API, with the CLI pointed at it and given a static token."""
    from celine.onboarding.config.settings import settings

    monkeypatch.setattr(settings, "onboarding_api_url", API)
    with respx.mock(base_url=API, assert_all_called=False) as mock:
        yield mock


def run(*args):
    return runner.invoke(app, ["admin", *args, "--token", "test-token"])


def queue_route(api, rows=None, total=None):
    body = [SUBMISSION] if rows is None else rows
    return api.get("/api/admin/rec-a/submissions").mock(
        return_value=httpx.Response(
            200,
            json=body,
            headers={"X-Total-Count": str(total if total is not None else len(body))},
        )
    )


# ---------------------------------------------------------------------------
# Transport wiring
# ---------------------------------------------------------------------------


class TestTransport:
    def test_the_token_is_sent(self, api):
        route = queue_route(api)
        run("review", "list", "--rec", "rec-a")
        assert route.calls.last.request.headers["Authorization"] == "Bearer test-token"

    def test_403_is_a_clean_message_not_a_traceback(self, api):
        api.get("/api/admin/rec-a/submissions").mock(
            return_value=httpx.Response(403, json={"detail": "no group grants this action"})
        )
        result = run("review", "list", "--rec", "rec-a")
        assert result.exit_code == 1
        assert "Not permitted" in result.output
        assert "Traceback" not in result.output

    def test_401_explains_what_to_check(self, api):
        api.get("/api/admin/rec-a/submissions").mock(return_value=httpx.Response(401))
        result = run("review", "list", "--rec", "rec-a")
        assert result.exit_code == 1
        assert "svc-onboarding-cli" in result.output


# ---------------------------------------------------------------------------
# Reference resolution
# ---------------------------------------------------------------------------


class TestReferenceResolution:
    def test_resolves_through_the_ref_filter(self, api):
        """The reference is what the participant was given; the UUID is not."""
        route = queue_route(api)
        api.get(f"/api/admin/rec-a/submissions/{SUBMISSION['id']}").mock(
            return_value=httpx.Response(200, json=SUBMISSION)
        )
        run("review", "show", "20260730-aaa1", "--rec", "rec-a")
        assert route.calls.last.request.url.params["ref"] == "20260730-aaa1"

    def test_an_ambiguous_prefix_is_refused(self, api):
        """Guessing which of five submissions was meant is not a service."""
        others = [dict(SUBMISSION, ref=f"20260730-aa{i}", id=f"id-{i}") for i in range(3)]
        queue_route(api, rows=others)
        result = run("review", "show", "2026", "--rec", "rec-a")
        assert result.exit_code == 1
        assert "matches 3 submissions" in result.output

    def test_an_exact_reference_wins_over_a_substring_match(self, api):
        """A full reference must resolve even when it is a prefix of others."""
        rows = [dict(SUBMISSION, ref="20260730-aaa10", id="other"), SUBMISSION]
        queue_route(api, rows=rows)
        detail = api.get(f"/api/admin/rec-a/submissions/{SUBMISSION['id']}").mock(
            return_value=httpx.Response(200, json=SUBMISSION)
        )
        result = run("review", "show", "20260730-aaa1", "--rec", "rec-a")
        assert result.exit_code == 0
        assert detail.called

    def test_no_match_says_so(self, api):
        queue_route(api, rows=[])
        result = run("review", "show", "NOPE", "--rec", "rec-a")
        assert result.exit_code == 1
        assert "No submission" in result.output


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


class TestReview:
    def test_list_reports_the_total(self, api):
        queue_route(api, total=137)
        result = run("review", "list", "--rec", "rec-a")
        assert "Showing 1 of 137" in result.output

    def test_list_json_is_machine_readable(self, api):
        queue_route(api, total=42)
        result = run("review", "list", "--rec", "rec-a", "--json")
        payload = json.loads(result.output)
        assert payload["total"] == 42
        assert payload["submissions"][0]["ref"] == "20260730-aaa1"

    def test_take_posts_the_transition(self, api):
        queue_route(api)
        route = api.post(f"/api/admin/rec-a/submissions/{SUBMISSION['id']}/transition").mock(
            return_value=httpx.Response(200, json=dict(SUBMISSION, status="under_review"))
        )

        result = run("review", "take", "20260730-aaa1", "--rec", "rec-a")
        assert result.exit_code == 0
        assert json.loads(route.calls.last.request.content)["target"] == "under_review"
        assert "submitted -> under_review" in result.output

    def test_reject_requires_a_reason_before_calling_the_api(self, api):
        """Refused locally, so a missing reason costs no round trip and no 422."""
        route = api.post(f"/api/admin/rec-a/submissions/{SUBMISSION['id']}/transition")
        result = run("review", "reject", "20260730-aaa1", "--rec", "rec-a")
        assert result.exit_code == 1
        assert "--reason is required" in result.output
        assert not route.called

    def test_reject_sends_the_reason(self, api):
        queue_route(api)
        route = api.post(f"/api/admin/rec-a/submissions/{SUBMISSION['id']}/transition").mock(
            return_value=httpx.Response(200, json=dict(SUBMISSION, status="rejected"))
        )

        run(
            "review",
            "reject",
            "20260730-aaa1",
            "--rec",
            "rec-a",
            "--reason",
            "POD belongs to another supply",
        )
        body = json.loads(route.calls.last.request.content)
        assert body["reason"] == "POD belongs to another supply"

    def test_approve_shows_the_pipeline(self, api):
        """The operator asked to enable somebody; what happened is the answer."""
        queue_route(api)
        api.post(f"/api/admin/rec-a/submissions/{SUBMISSION['id']}/transition").mock(
            return_value=httpx.Response(200, json=dict(SUBMISSION, status="approved"))
        )
        api.get(f"/api/admin/rec-a/submissions/{SUBMISSION['id']}/enablement").mock(
            return_value=httpx.Response(200, json=ENABLEMENT)
        )
        result = run("review", "approve", "20260730-aaa1", "--rec", "rec-a")
        assert "rec_registry_member" in result.output
        assert "registry unreachable" in result.output

    def test_a_blocked_approval_shows_why(self, api):
        queue_route(api)
        api.post(f"/api/admin/rec-a/submissions/{SUBMISSION['id']}/transition").mock(
            return_value=httpx.Response(
                422, json={"detail": "Community member could not be provisioned"}
            )
        )
        api.get(f"/api/admin/rec-a/submissions/{SUBMISSION['id']}/enablement").mock(
            return_value=httpx.Response(200, json=ENABLEMENT)
        )
        result = run("review", "approve", "20260730-aaa1", "--rec", "rec-a")
        assert result.exit_code == 1
        assert "Approval blocked" in result.output
        assert "registry unreachable" in result.output

    def test_a_refusal_before_the_pipeline_shows_no_step_table(self, api):
        """An unverified phone stops approval before enablement starts.

        Printing untouched steps would suggest they were tried and failed.
        """
        queue_route(api)
        api.post(f"/api/admin/rec-a/submissions/{SUBMISSION['id']}/transition").mock(
            return_value=httpx.Response(
                422, json={"detail": "Cannot approve: phone number is not verified"}
            )
        )
        api.get(f"/api/admin/rec-a/submissions/{SUBMISSION['id']}/enablement").mock(
            return_value=httpx.Response(200, json=dict(ENABLEMENT, state="not_started", steps=[]))
        )
        result = run("review", "approve", "20260730-aaa1", "--rec", "rec-a")
        assert result.exit_code == 1
        assert "phone number is not verified" in result.output
        assert "Approval blocked" not in result.output

    def test_reveal_is_opt_in(self, api):
        queue_route(api)
        route = api.get(f"/api/admin/rec-a/submissions/{SUBMISSION['id']}").mock(
            return_value=httpx.Response(200, json=SUBMISSION)
        )
        run("review", "show", "20260730-aaa1", "--rec", "rec-a")
        assert route.calls.last.request.url.params["reveal"] == "false"

        run("review", "show", "20260730-aaa1", "--rec", "rec-a", "--reveal")
        assert route.calls.last.request.url.params["reveal"] == "true"


# ---------------------------------------------------------------------------
# Enablement
# ---------------------------------------------------------------------------


class TestEnablement:
    def test_status_prints_the_pipeline(self, api):
        queue_route(api)
        api.get(f"/api/admin/rec-a/submissions/{SUBMISSION['id']}/enablement").mock(
            return_value=httpx.Response(200, json=ENABLEMENT)
        )
        result = run("enablement", "status", "20260730-aaa1", "--rec", "rec-a")
        assert "state: failed" in result.output
        assert "(non-blocking)" not in result.output  # both steps here are closed

    def test_retry_passes_the_step(self, api):
        queue_route(api)
        route = api.post(f"/api/admin/rec-a/submissions/{SUBMISSION['id']}/enablement/retry").mock(
            return_value=httpx.Response(200, json=dict(ENABLEMENT, state="complete"))
        )

        run(
            "enablement",
            "retry",
            "20260730-aaa1",
            "--rec",
            "rec-a",
            "--step",
            "rec_registry_member",
        )
        assert json.loads(route.calls.last.request.content)["step"] == "rec_registry_member"

    def test_retry_exits_nonzero_when_it_still_fails(self, api):
        """So a repair loop in a script can tell whether it worked."""
        queue_route(api)
        api.post(f"/api/admin/rec-a/submissions/{SUBMISSION['id']}/enablement/retry").mock(
            return_value=httpx.Response(200, json=ENABLEMENT)
        )
        result = run("enablement", "retry", "20260730-aaa1", "--rec", "rec-a")
        assert result.exit_code == 1

    def test_retry_exits_zero_when_it_worked(self, api):
        queue_route(api)
        api.post(f"/api/admin/rec-a/submissions/{SUBMISSION['id']}/enablement/retry").mock(
            return_value=httpx.Response(200, json=dict(ENABLEMENT, state="complete"))
        )
        result = run("enablement", "retry", "20260730-aaa1", "--rec", "rec-a")
        assert result.exit_code == 0

    def test_revoke_needs_confirmation(self, api):
        route = api.post(f"/api/admin/rec-a/submissions/{SUBMISSION['id']}/enablement/revoke")
        result = run("enablement", "revoke", "20260730-aaa1", "--rec", "rec-a")
        assert result.exit_code == 1
        assert not route.called

    def test_revoke_with_confirmation_calls_the_api(self, api):
        queue_route(api)
        route = api.post(f"/api/admin/rec-a/submissions/{SUBMISSION['id']}/enablement/revoke").mock(
            return_value=httpx.Response(200, json=dict(ENABLEMENT, state="not_started"))
        )
        result = run("enablement", "revoke", "20260730-aaa1", "--rec", "rec-a", "--confirm")
        assert result.exit_code == 0
        assert route.called


# ---------------------------------------------------------------------------
# Purge and audit
# ---------------------------------------------------------------------------


class TestPurgeAndAudit:
    def test_purge_needs_confirmation(self, api):
        route = api.delete(f"/api/admin/rec-a/submissions/{SUBMISSION['id']}")
        result = run("purge", "20260730-aaa1", "--rec", "rec-a")
        assert result.exit_code == 1
        assert not route.called

    def test_purge_with_confirmation(self, api):
        queue_route(api)
        route = api.delete(f"/api/admin/rec-a/submissions/{SUBMISSION['id']}").mock(
            return_value=httpx.Response(204)
        )
        result = run("purge", "20260730-aaa1", "--rec", "rec-a", "--confirm")
        assert result.exit_code == 0
        assert route.called

    def test_audit_filters_by_action(self, api):
        api.get("/api/admin/rec-a/audit-logs").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "created_at": "2026-07-30T10:00:00Z",
                        "action": "transition",
                        "actor_type": "user",
                        "actor_sub": "kc-1",
                        "actor_email": "op@example.org",
                        "detail": "approved",
                    },
                    {
                        "created_at": "2026-07-30T09:00:00Z",
                        "action": "reveal",
                        "actor_type": "user",
                        "actor_sub": "kc-2",
                        "actor_email": "other@example.org",
                        "detail": None,
                    },
                ],
            )
        )
        result = run("audit", "--rec", "rec-a", "--action", "reveal")
        assert "other@example.org" in result.output
        assert "op@example.org" not in result.output


# ---------------------------------------------------------------------------
# --local
# ---------------------------------------------------------------------------


class TestLocalMode:
    def test_local_is_refused_unless_asked_for(self, monkeypatch):
        """It bypasses authorization entirely, so it cannot be the accidental path."""
        from celine.onboarding.config.settings import settings

        monkeypatch.setattr(settings, "allow_local_admin", False)
        result = runner.invoke(app, ["admin", "whoami", "--local"])
        assert result.exit_code == 1
        assert "ALLOW_LOCAL_ADMIN" in result.output

    def test_local_reports_itself_as_the_cli(self, monkeypatch, seed_rec):
        from celine.onboarding.config.settings import settings
        from celine.onboarding.services import template_service

        monkeypatch.setattr(settings, "allow_local_admin", True)
        seed_rec("rec-a", name="REC A", organization="community-a")

        async def _noop():
            return None

        monkeypatch.setattr(template_service, "load_recs_from_db", _noop)

        result = runner.invoke(app, ["admin", "whoami", "--local", "--json"])
        payload = json.loads(result.output)
        assert payload["subject_type"] == "cli"
        assert "@" in payload["sub"]
        assert payload["recs"][0]["slug"] == "rec-a"
