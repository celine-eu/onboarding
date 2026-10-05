"""The member's own login token on ds's person routes (ds ADR-0024, R3).

`/consent/my/shares` (read and write) and `/prov/my/events` bind the credential to
the person presenting it: the member's own Keycloak token travels beside their
credential, and this service's token never does.
"""

from __future__ import annotations

import httpx
from test_member_sharing import (  # reuse the established harness
    OFFER_CONSENT,
    VC,
    _dataspace,  # noqa: F401 — a fixture, used by name
    _handler,
    _member,
    _patch_httpx,
)

import celine.onboarding.services.dataspace_identity as di
import celine.onboarding.services.member_sharing as ms

MEMBER_TOKEN = "member-login-token"
PERSON_ROUTES = ("/consent/my/shares", "/prov/my/events")


def _logged_in_member():
    member = _member()
    member.token = MEMBER_TOKEN
    return member


def _recording(monkeypatch, *, provenance: bool = False):
    if provenance:
        monkeypatch.setattr(ms.settings, "ds_provenance_url", "http://prov:8000")
    seen: list[httpx.Request] = []
    inner = _handler()

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return inner(req)

    _patch_httpx(monkeypatch, handle)
    return seen


def _person_calls(seen: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in seen if r.url.path in PERSON_ROUTES]


def test_the_headers_carry_the_credential_and_the_members_login():
    """
    @verifies REQ-0039
    """
    credential = di.SubjectCredential(subject_id="did:x", vc_jws=VC)

    assert ms.member_headers(credential, _logged_in_member()) == {
        "X-Subject-Id": "did:x",
        "X-User-VC": VC,
        "Authorization": f"Bearer {MEMBER_TOKEN}",
    }
    # No login on the request: nothing is substituted for it.
    assert "Authorization" not in ms.member_headers(credential, _member())


async def test_reading_the_decisions_sends_the_members_token(monkeypatch, bind_rec, _dataspace):  # noqa: F811
    """
    @verifies REQ-0039
    """
    seen = _recording(monkeypatch)

    await ms.get_data_sharing(_logged_in_member())

    calls = _person_calls(seen)
    assert [(r.method, r.url.path) for r in calls] == [("GET", "/consent/my/shares")]
    assert calls[0].headers["authorization"] == f"Bearer {MEMBER_TOKEN}"
    assert calls[0].headers["x-user-vc"] == VC


async def test_deciding_sends_the_members_token(monkeypatch, bind_rec, _dataspace):  # noqa: F811
    """The write, and the read that renders the page after it.

    @verifies REQ-0039
    """
    seen = _recording(monkeypatch)

    await ms.set_data_sharing(_logged_in_member(), OFFER_CONSENT["id"], enabled=True)

    calls = _person_calls(seen)
    assert [(r.method, r.url.path) for r in calls] == [
        ("POST", "/consent/my/shares"),
        ("GET", "/consent/my/shares"),
    ]
    assert {r.headers["authorization"] for r in calls} == {f"Bearer {MEMBER_TOKEN}"}


async def test_the_history_sends_the_members_token(monkeypatch, bind_rec, _dataspace):  # noqa: F811
    """
    @verifies REQ-0039
    """
    seen = _recording(monkeypatch, provenance=True)

    await ms.get_history(_logged_in_member())

    calls = _person_calls(seen)
    assert [r.url.path for r in calls] == ["/prov/my/events"]
    assert calls[0].headers["authorization"] == f"Bearer {MEMBER_TOKEN}"


async def test_this_services_token_never_reaches_a_person_route(
    monkeypatch,
    bind_rec,
    _dataspace,  # noqa: F811
):
    """The service token is `test-token` in this harness: it goes to the registry only.

    @verifies REQ-0039
    """
    seen = _recording(monkeypatch, provenance=True)
    member = _logged_in_member()

    await ms.get_data_sharing(member)
    await ms.set_data_sharing(member, OFFER_CONSENT["id"], enabled=False)
    await ms.get_history(member)

    person = _person_calls(seen)
    assert len(person) == 4
    assert all(r.headers.get("authorization") != "Bearer test-token" for r in person)
    assert any(r.headers.get("authorization") == "Bearer test-token" for r in seen)
