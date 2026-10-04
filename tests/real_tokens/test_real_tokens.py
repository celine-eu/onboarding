"""The two levels of grant, with tokens a real realm issued (REQ-0030).

An organization's `admins` is not a platform admin; a holder of the `platform-admin`
role is; a realm group still present in a token grants nothing. Each answer comes from
the app's own `/api/admin/**` routes, verifying the token against the realm's JWKS.
"""

from __future__ import annotations

import base64
import json

import pytest

from celine.onboarding.security.policy import ALL_CAPABILITIES

PLATFORM_ADMIN_ROLE = "platform-admin"
VIEWER = {"recs.read", "submissions.read", "audit.read"}
ORG_ADMIN = {c.value for c in ALL_CAPABILITIES} - {"members.invite", "recs.write"}
PLATFORM_ADMIN = ORG_ADMIN | {"recs.write"}
SERVICE_ADMIN = ORG_ADMIN - {"recs.drift"}
ALL_RECS = ["foreign-rec", "own-rec", "unbound-rec"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def claims_of(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def capabilities(body: dict) -> dict[str, set[str]]:
    return {rec["slug"]: set(rec["capabilities"]) for rec in body["recs"]}


def reload_status(client, token: str, monkeypatch) -> int:
    from celine.onboarding.services import template_service

    async def _noop():
        return None

    monkeypatch.setattr(template_service, "reload", _noop)
    return client.post("/api/admin/recs/reload", headers=auth(token)).status_code


def test_a_platform_admin_holds_everything_everywhere(client, user_token, monkeypatch):
    """
    @verifies REQ-0030
    """
    token = user_token("ONBOARDING_KC_PLATFORM_ADMIN")
    assert PLATFORM_ADMIN_ROLE in claims_of(token).get("realm_access", {}).get("roles", [])

    response = client.get("/api/admin/me", headers=auth(token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["subject_type"] == "user"
    assert PLATFORM_ADMIN_ROLE in body["platform_roles"]
    assert capabilities(body) == {slug: PLATFORM_ADMIN for slug in ALL_RECS}
    assert reload_status(client, token, monkeypatch) == 200


def test_an_organization_admin_is_not_a_platform_admin(
    client, user_token, organization, monkeypatch
):
    """
    @verifies REQ-0030
    """
    token = user_token("ONBOARDING_KC_ORG_ADMIN")
    claims = claims_of(token)
    assert PLATFORM_ADMIN_ROLE not in claims.get("realm_access", {}).get("roles", [])
    assert claims["organization"][organization]["groups"] == ["/admins"]

    body = client.get("/api/admin/me", headers=auth(token)).json()
    assert PLATFORM_ADMIN_ROLE not in body["platform_roles"]
    assert capabilities(body) == {"own-rec": ORG_ADMIN}
    assert reload_status(client, token, monkeypatch) == 403
    sync = client.post("/api/admin/recs/own-rec/registry-sync?dry_run=true", headers=auth(token))
    assert sync.status_code == 403
    assert "platform-admin" in sync.json()["detail"]


def test_an_organization_viewer_reads_only_their_community(client, user_token):
    """
    @verifies REQ-0030
    """
    token = user_token("ONBOARDING_KC_ORG_VIEWER")
    body = client.get("/api/admin/me", headers=auth(token)).json()
    assert capabilities(body) == {"own-rec": VIEWER}


def test_a_realm_group_in_a_real_token_grants_nothing(client, legacy_token, monkeypatch):
    """The platform admin before the role: a realm `/admins` group in the token.

    @verifies REQ-0030
    """
    token = legacy_token
    claims = claims_of(token)
    groups = [g.lstrip("/") for g in claims.get("groups") or []]
    assert "admins" in groups, "the legacy token carries no realm admins group"
    assert PLATFORM_ADMIN_ROLE not in claims.get("realm_access", {}).get("roles", [])

    response = client.get("/api/admin/me", headers=auth(token))
    if response.status_code == 403:
        # No organization here: the realm group alone is nothing.
        pass
    else:
        assert response.status_code == 200, response.text
        # Whatever it holds comes from its organization, never from the realm group.
        assert "foreign-rec" not in capabilities(response.json())
        assert "unbound-rec" not in capabilities(response.json())
        for caps in capabilities(response.json()).values():
            assert "recs.write" not in caps
    assert reload_status(client, token, monkeypatch) == 403


def test_a_service_is_judged_by_its_scopes(client, service_access_token):
    """
    @verifies REQ-0030
    """
    body = client.get("/api/admin/me", headers=auth(service_access_token)).json()
    assert body["subject_type"] == "service"
    assert capabilities(body) == {slug: SERVICE_ADMIN for slug in ALL_RECS}


@pytest.mark.parametrize("variable", ["ONBOARDING_KC_PLATFORM_ADMIN", "ONBOARDING_KC_ORG_ADMIN"])
def test_no_realm_issued_token_carries_a_realm_group(user_token, variable):
    """The realm side of REQ-0030: a converged realm emits no top-level `groups`."""
    assert "groups" not in claims_of(user_token(variable))
