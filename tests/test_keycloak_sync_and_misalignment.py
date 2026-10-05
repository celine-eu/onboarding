"""The login binding as the community's act, and a 404 on a delete as a misalignment.

What is asserted here:

- `POST /admin/keycloak/sync` — the (realm, Keycloak user id) -> DID mapping a
  person route's login binding reads (ds ADR-0024) — is made as the community's
  collector client, asking for `identity-registry.keycloak.sync` alone, both at
  approval and when a member's email correction re-syncs it; under
  `CELINE_ENV=dev` without a collector secret it stays this service's own call,
  warned (ds ADR-0026, amended 2026-10-05);
- a 404 on a membership delete or a credential revocation is still the state a
  delete wants, and is logged as a **misalignment** — onboarding recorded
  something the registry does not hold — naming the community and the
  submission reference, never the email or the DID.
"""

from __future__ import annotations

import logging

import httpx
import pytest
import test_collector_client as collector_suite
from test_collector_client import (
    ALIAS,
    CLIENT_ID,
    ENV_NAME,
    _bearer,
    _named_token_provider,
    _patch_httpx,
)

import celine.onboarding.services.dataspace_identity as di
from celine.onboarding.services import service_auth

# The collector suite's fixtures, shared rather than copied.
_clean = collector_suite._clean
tokens = collector_suite.tokens
bound = collector_suite.bound

EMAIL = "someone@example.org"
DID = "did:web:rec.example:users:subject-1"


@pytest.fixture()
def submission(submission):
    submission.email = EMAIL
    return submission


# ── the login binding ──────────────────────────────────────────────


class TestTheLoginBindingIsTheCommunitysAct:
    """
    @verifies REQ-0035
    """

    async def test_the_sync_token_asks_for_the_sync_scope_alone(self, monkeypatch, tokens):
        """
        @verifies REQ-0036
        """
        monkeypatch.setenv(ENV_NAME, "s3cret")

        headers = await di.keycloak_sync_headers(ALIAS)

        assert headers == {"Authorization": _bearer(service_auth.KEYCLOAK_SYNC)}
        assert tokens == [(CLIENT_ID, "s3cret", service_auth.KEYCLOAK_SYNC)]
        assert service_auth.KEYCLOAK_SYNC in service_auth.COLLECTOR_SCOPES

    async def test_the_sync_token_is_fetched_before_anything_is_issued(
        self, monkeypatch, tokens, bound, submission
    ):
        """A realm that does not grant the scope yet refuses the token; nothing is
        issued that a failed sync would then have to roll back.

        @verifies REQ-0035
        """
        monkeypatch.setenv(ENV_NAME, "s3cret")
        real = service_auth.collector_token_provider

        def refusing(alias, scope):
            if scope == service_auth.KEYCLOAK_SYNC:
                raise RuntimeError("invalid_scope")
            return real(alias, scope)

        monkeypatch.setattr(service_auth, "collector_token_provider", refusing)
        calls = []

        def handler(req):
            calls.append(req.url.path)
            if req.url.path == "/users/resolve":
                return httpx.Response(200, json={"subject_id": "subject-1"})
            raise AssertionError(f"unexpected {req.url}")

        _patch_httpx(monkeypatch, handler)

        with pytest.raises(RuntimeError, match="invalid_scope"):
            await di.provision_user_identity(
                submission,
                keycloak_user_id="kc-1",
                keycloak_realm="celine",
                provision_shares=False,
            )
        assert "/admin/credentials/data-subject" not in calls

    async def test_in_dev_without_a_secret_it_stays_this_services_own(
        self, monkeypatch, tokens, caplog
    ):
        """
        @verifies REQ-0037
        """
        monkeypatch.setenv("CELINE_ENV", "dev")

        with caplog.at_level(logging.WARNING, logger="celine.onboarding.services.service_auth"):
            headers = await di.keycloak_sync_headers(ALIAS)

        assert headers == {"Authorization": "Bearer service"}
        assert any(
            ENV_NAME in r.getMessage() and service_auth.KEYCLOAK_SYNC in r.getMessage()
            for r in caplog.records
        )

    async def test_an_email_correction_re_syncs_as_the_collector(
        self, monkeypatch, tokens, bound, submission
    ):
        """`propagation._identity_mapping`: the same DID, the new address, as the community.

        @verifies REQ-0036
        """
        from celine.onboarding.services import propagation, provisioning

        monkeypatch.setenv(ENV_NAME, "s3cret")
        monkeypatch.setattr(provisioning, "keycloak_realm", lambda: "celine")
        submission.dataspace_did = DID
        seen = []

        def handler(req):
            seen.append((req.method, req.url.path, req.headers.get("authorization")))
            return httpx.Response(200, json={"status": "synced"})

        _patch_httpx(monkeypatch, handler)

        ctx = propagation.RunContext(
            submission=submission,
            revision=None,
            rows={},
            keycloak_user_id="kc-1",
            username="someone",
        )
        result = await propagation._identity_mapping(ctx)

        assert result.status == propagation.StepStatus.DONE
        assert seen == [("POST", "/admin/keycloak/sync", _bearer(service_auth.KEYCLOAK_SYNC))]


# ── a 404 on a delete is a misalignment ────────────────────────────


def _misalignments(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "MISALIGNMENT" in r.getMessage()
    ]


class TestA404OnADeleteIsAMisalignment:
    """
    @verifies REQ-0040
    """

    async def test_a_missing_membership_is_logged_by_reference_and_still_succeeds(
        self, monkeypatch, caplog
    ):
        """
        @verifies REQ-0040
        """
        _patch_httpx(monkeypatch, lambda req: httpx.Response(404))

        with caplog.at_level(logging.WARNING):
            await di._delete_membership("http://ir", {}, DID, ALIAS, subject_ref="20261005-abc123")

        (line,) = _misalignments(caplog)
        assert "a membership" in line
        assert ALIAS in line and "20261005-abc123" in line
        assert DID not in line

    @pytest.mark.parametrize("status", [200, 204])
    async def test_a_removed_membership_is_no_misalignment(self, monkeypatch, caplog, status):
        """
        @verifies REQ-0040
        """
        _patch_httpx(monkeypatch, lambda req: httpx.Response(status))

        with caplog.at_level(logging.WARNING):
            await di._delete_membership("http://ir", {}, DID, ALIAS, subject_ref="ref-1")

        assert _misalignments(caplog) == []

    async def test_revocation_names_both_misalignments_and_never_the_person(
        self, monkeypatch, tokens, bound, submission, caplog
    ):
        """Membership and credential both already gone: success, two warnings.

        @verifies REQ-0040
        """
        monkeypatch.setenv(ENV_NAME, "s3cret")
        submission.dataspace_vc_id = "urn:uuid:cred-001"
        submission.dataspace_did = DID

        _patch_httpx(monkeypatch, lambda req: httpx.Response(404))

        with caplog.at_level(logging.WARNING):
            outcome = await di.revoke_user_identity(submission)

        assert outcome == "revoked credential urn:uuid:cred-001"
        assert submission.dataspace_vc_id is None
        lines = _misalignments(caplog)
        assert len(lines) == 2
        assert "a membership" in lines[0]
        assert "credential urn:uuid:cred-001" in lines[1]
        for line in lines:
            assert submission.ref in line and ALIAS in line
            assert EMAIL not in line and DID not in line

    async def test_the_rollback_logs_a_credential_already_gone(self, monkeypatch, caplog):
        """
        @verifies REQ-0040
        """
        di._token_provider = _named_token_provider("service")

        def handler(req):
            if req.url.path == "/admin/keycloak/sync":
                return httpx.Response(500, text="keycloak down")
            if req.url.path.startswith("/admin/credentials/"):
                return httpx.Response(404)
            return httpx.Response(204)

        _patch_httpx(monkeypatch, handler)

        with caplog.at_level(logging.WARNING), pytest.raises(ValueError, match="has been revoked"):
            await di._sync_keycloak(
                "http://ir",
                {},
                did=DID,
                keycloak_user_id="kc-1",
                keycloak_realm="celine",
                email=EMAIL,
                credential_id="urn:uuid:cred-001",
                organization_alias=ALIAS,
            )

        (line,) = _misalignments(caplog)
        assert "credential urn:uuid:cred-001" in line
        assert EMAIL not in line and DID not in line
