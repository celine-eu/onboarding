"""The console API against a live app, a real database and real signed tokens.

The unit suite proves each layer in isolation; this proves they are wired
together — that a token really is verified against a JWKS over HTTP, that the
policy really is consulted per request, that a rejection really does reach
Postgres, and that the anonymous wizard still works with all of it in place.
"""

from __future__ import annotations

import httpx
import pytest

from .conftest import ORG, OTHER_ORG, OTHER_REC, REC

pytestmark = pytest.mark.e2e


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


class TestGate:
    def test_the_wizard_is_still_anonymous(self, client: httpx.Client):
        """The console shares this process. Breaking the public path is the risk."""
        assert client.get(f"/api/{REC}/config").status_code == 200
        assert client.get("/api/recs").status_code == 200
        assert client.get("/api/health").status_code == 200

    def test_no_token_is_401_from_the_app(self, client: httpx.Client):
        """Not from the ingress — the service enforces its own gate.

        Caddy's forward_auth buys the browser login flow; if it were also the
        authorization, anything bypassing it would be unauthenticated.
        """
        response = client.get("/api/admin/me")
        assert response.status_code == 401
        assert "location" not in {k.lower() for k in response.headers}

    def test_a_token_from_another_issuer_is_401(self, client: httpx.Client):
        from .idp import TestIdp

        other = TestIdp().start()
        try:
            token = other.operator(ORG, "admins")
            assert client.get("/api/admin/me", headers=auth(token)).status_code == 401
        finally:
            other.stop()

    def test_an_unsigned_token_is_401(self, client: httpx.Client, idp):
        import time

        import jwt as pyjwt

        forged = pyjwt.encode(
            {
                "iss": idp.issuer,
                "aud": "svc-onboarding",
                "sub": "attacker",
                "exp": int(time.time()) + 300,
                "groups": ["/admins"],
            },
            key="",
            algorithm="none",
        )
        assert client.get("/api/admin/me", headers=auth(forged)).status_code == 401


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


class TestTenancy:
    def test_me_lists_only_the_callers_community(self, client: httpx.Client, idp):
        body = client.get("/api/admin/me", headers=auth(idp.operator(ORG, "viewers"))).json()
        assert [r["slug"] for r in body["recs"]] == [REC]

    def test_an_operator_cannot_read_another_community(self, client: httpx.Client, idp):
        response = client.get(
            f"/api/admin/{OTHER_REC}/submissions", headers=auth(idp.operator(ORG, "admins"))
        )
        assert response.status_code == 403
        assert "different organization" in response.json()["detail"]

    def test_a_realm_operator_sees_both(self, client: httpx.Client, idp):
        body = client.get(
            "/api/admin/me", headers=auth(idp.operator(ORG, realm=("admins",)))
        ).json()
        assert {r["slug"] for r in body["recs"]} >= {REC, OTHER_REC}

    def test_a_submission_is_not_reachable_across_communities(
        self, client: httpx.Client, idp, submission
    ):
        """404, not 403: whether it exists elsewhere is itself information."""
        made = submission(REC)
        response = client.get(
            f"/api/admin/{OTHER_REC}/submissions/{made['id']}",
            headers=auth(idp.operator(OTHER_ORG, "admins")),
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class TestCapabilities:
    @pytest.mark.parametrize(
        "tier,expected",
        [("viewers", 403), ("editors", 403), ("managers", 200), ("admins", 200)],
    )
    def test_who_may_approve(self, client: httpx.Client, idp, submission, tier, expected):
        made = submission(REC)
        response = client.post(
            f"/api/admin/{REC}/submissions/{made['id']}/transition",
            headers=auth(idp.operator(ORG, tier)),
            json={"target": "approved"},
        )
        assert response.status_code == expected

    @pytest.mark.parametrize("tier,expected", [("managers", 403), ("admins", 204)])
    def test_who_may_erase(self, client: httpx.Client, idp, submission, tier, expected):
        """Erasure is separated from review because it is not recoverable."""
        made = submission(REC)
        response = client.delete(
            f"/api/admin/{REC}/submissions/{made['id']}",
            headers=auth(idp.operator(ORG, tier)),
        )
        assert response.status_code == expected

    def test_a_service_account_is_authorised_by_scope(self, client: httpx.Client, idp, submission):
        made = submission(REC)
        narrow = auth(idp.service("onboarding.submissions.read"))
        assert (
            client.get(f"/api/admin/{REC}/submissions/{made['id']}", headers=narrow).status_code
            == 200
        )
        assert (
            client.delete(f"/api/admin/{REC}/submissions/{made['id']}", headers=narrow).status_code
            == 403
        )


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------


class TestMasking:
    def test_identifiers_are_masked_by_default(self, client: httpx.Client, idp, submission):
        made = submission(REC)
        body = client.get(
            f"/api/admin/{REC}/submissions/{made['id']}", headers=auth(idp.operator(ORG, "admins"))
        ).json()
        assert body["fiscal_code"].endswith("562S")
        assert "•" in body["fiscal_code"]

    def test_a_viewer_cannot_reveal(self, client: httpx.Client, idp, submission):
        made = submission(REC)
        response = client.get(
            f"/api/admin/{REC}/submissions/{made['id']}?reveal=true",
            headers=auth(idp.operator(ORG, "viewers")),
        )
        assert response.status_code == 403

    def test_reveal_returns_the_real_value_and_is_audited(
        self, client: httpx.Client, idp, submission
    ):
        made = submission(REC)
        headers = auth(idp.operator(ORG, "editors"))
        body = client.get(
            f"/api/admin/{REC}/submissions/{made['id']}?reveal=true", headers=headers
        ).json()
        assert body["fiscal_code"] == "RSSMRA85T10A562S"

        trail = client.get(f"/api/admin/{REC}/audit-logs", headers=headers).json()
        reveals = [e for e in trail if e["action"] == "reveal" and e["entity_id"] == made["id"]]
        assert reveals, "the reveal was not recorded"
        assert reveals[0]["actor_email"] == "operator@example.org"


# ---------------------------------------------------------------------------
# Approval and enablement
# ---------------------------------------------------------------------------


class TestApproval:
    def test_approval_records_every_step(self, client: httpx.Client, idp, submission):
        made = submission(REC)
        headers = auth(idp.operator(ORG, "managers"))

        approved = client.post(
            f"/api/admin/{REC}/submissions/{made['id']}/transition",
            headers=headers,
            json={"target": "approved"},
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"

        enablement = client.get(
            f"/api/admin/{REC}/submissions/{made['id']}/enablement", headers=headers
        ).json()
        # Nothing is configured in this environment, so every step legitimately
        # skips — and "skipped" is what the pipeline must record, not "failed".
        assert [s["step"] for s in enablement["steps"]] == [
            "keycloak_user",
            "rec_registry_member",
            "dataspace_identity",
            "dataspace_share",
        ]
        assert enablement["state"] == "complete"
        assert all(s["status"] == "skipped" for s in enablement["steps"])

    def test_rejection_requires_a_reason_and_keeps_it(self, client: httpx.Client, idp, submission):
        made = submission(REC)
        headers = auth(idp.operator(ORG, "managers"))

        refused = client.post(
            f"/api/admin/{REC}/submissions/{made['id']}/transition",
            headers=headers,
            json={"target": "rejected"},
        )
        assert refused.status_code == 422

        client.post(
            f"/api/admin/{REC}/submissions/{made['id']}/transition",
            headers=headers,
            json={"target": "rejected", "reason": "POD di un'altra fornitura"},
        )
        trail = client.get(f"/api/admin/{REC}/audit-logs", headers=headers).json()
        entry = next(
            e for e in trail if e["action"] == "transition" and e["entity_id"] == made["id"]
        )
        assert "POD di un'altra fornitura" in entry["detail"]

    def test_an_illegal_transition_is_refused(self, client: httpx.Client, idp, submission):
        made = submission(REC)
        headers = auth(idp.operator(ORG, "managers"))
        client.post(
            f"/api/admin/{REC}/submissions/{made['id']}/transition",
            headers=headers,
            json={"target": "approved"},
        )
        # APPROVED is terminal.
        again = client.post(
            f"/api/admin/{REC}/submissions/{made['id']}/transition",
            headers=headers,
            json={"target": "rejected", "reason": "changed my mind"},
        )
        assert again.status_code == 422

    def test_retry_is_idempotent_on_a_finished_pipeline(
        self, client: httpx.Client, idp, submission
    ):
        made = submission(REC)
        headers = auth(idp.operator(ORG, "managers"))
        client.post(
            f"/api/admin/{REC}/submissions/{made['id']}/transition",
            headers=headers,
            json={"target": "approved"},
        )
        before = client.get(
            f"/api/admin/{REC}/submissions/{made['id']}/enablement", headers=headers
        ).json()
        after = client.post(
            f"/api/admin/{REC}/submissions/{made['id']}/enablement/retry",
            headers=headers,
            json={},
        ).json()
        # Skipped steps are not re-run, so the attempt counts do not move.
        assert [s["attempts"] for s in after["steps"]] == [s["attempts"] for s in before["steps"]]


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


class TestQueue:
    def test_pagination_reports_a_total(self, client: httpx.Client, idp, submission):
        for _ in range(3):
            submission(REC)
        headers = auth(idp.operator(ORG, "viewers"))
        response = client.get(f"/api/admin/{REC}/submissions?limit=2", headers=headers)
        assert len(response.json()) == 2
        assert int(response.headers["X-Total-Count"]) >= 3

    def test_the_reference_filter_narrows_both_page_and_total(
        self, client: httpx.Client, idp, submission
    ):
        made = submission(REC)
        headers = auth(idp.operator(ORG, "viewers"))
        response = client.get(f"/api/admin/{REC}/submissions?ref={made['ref']}", headers=headers)
        assert [s["ref"] for s in response.json()] == [made["ref"]]
        assert response.headers["X-Total-Count"] == "1"

    def test_stats_count_the_communitys_own_rows(self, client: httpx.Client, idp, submission):
        submission(REC)
        headers = auth(idp.operator(ORG, "viewers"))
        stats = client.get(f"/api/admin/{REC}/stats", headers=headers).json()
        assert stats["rec_slug"] == REC
        assert set(stats["by_status"]) == {
            "draft",
            "submitted",
            "under_review",
            "approved",
            "rejected",
        }


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class TestAudit:
    def test_actions_are_attributed_to_the_operator(self, client: httpx.Client, idp, submission):
        made = submission(REC)
        headers = auth(idp.operator(ORG, "managers", sub="alice", email="alice@example.org"))
        client.post(
            f"/api/admin/{REC}/submissions/{made['id']}/transition",
            headers=headers,
            json={"target": "approved"},
        )
        trail = client.get(f"/api/admin/{REC}/audit-logs", headers=headers).json()
        entry = next(
            e for e in trail if e["entity_id"] == made["id"] and e["action"] == "transition"
        )
        assert entry["actor_type"] == "user"
        assert entry["actor_sub"] == "alice"
        assert entry["actor_email"] == "alice@example.org"

    def test_the_trail_is_scoped_to_one_community(self, client: httpx.Client, idp, submission):
        submission(REC)
        submission(OTHER_REC)
        trail = client.get(
            f"/api/admin/{REC}/audit-logs", headers=auth(idp.operator(ORG, "viewers"))
        ).json()
        assert trail, "expected some entries"
        assert {e["rec_slug"] for e in trail} == {REC}
