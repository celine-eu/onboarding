"""The member's own data-sharing surface.

`the-wizard-is-where-a-member-becomes-a-subject`, Phase 2. What is asserted here
is mostly the *distinctions*: the states this replaces were one state, and the
whole reason the feature was useless is that "your community is not in a
dataspace" and "you have no credential yet" arrived as the same sentence.

Two properties are load-bearing and asserted directly rather than left implied:
no response ever carries a credential, and no route consults a capability.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import celine.onboarding.services.dataspace_identity as di
import celine.onboarding.services.member_sharing as ms
from celine.onboarding.services import template_service

_OriginalAsyncClient = httpx.AsyncClient

OFFER_CONSENT = {
    "id": "household-energy-flexibility",
    "requires_consent": True,
    "controller": "rec-example",
}
OFFER_CONTRACT = {
    "id": "dso-statutory-reporting",
    "requires_consent": False,
    "controller": "dso",
}

VC = "eyJhbGciOiJFZERTQSJ9.stub-credential-jws"
RESOLVE_WITH_CREDENTIAL = {
    "subject_id": "email-abc123",
    "did": "did:web:users.example:email-abc123",
    "role": "DataSubject",
    "vc_jws": VC,
    "credentials": [{"role": "DataSubject", "vc_jws": VC}],
}


@pytest.fixture(autouse=True)
def _reset_token_provider():
    di._token_provider = None
    yield
    di._token_provider = None


def _mock_token_provider():
    token = MagicMock()
    token.access_token = "test-token"
    token.is_valid.return_value = True
    provider = AsyncMock()
    provider.get_token.return_value = token
    return provider


def _patch_httpx(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    def factory(**kw):
        kw.pop("transport", None)
        return _OriginalAsyncClient(transport=transport, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _member(organization: str | None = "rec-example", email: str | None = "member@example.org"):
    """A plain member: an organization membership and no groups at all."""
    from celine.sdk.auth import JwtUser

    claims: dict = {"sub": "member-sub", "email": email}
    if organization:
        claims["organization"] = {organization: {"id": "org-uuid", "groups": []}}
    return JwtUser(sub="member-sub", email=email, claims=claims)


@pytest.fixture()
def _dataspace(monkeypatch, bind_rec):
    # `bind_rec` is called for its `ensure_fresh` stub; the manifest is then
    # written in full because these tests need the `consent.data_sharing` block
    # `get_sharing_offers` reads, which `bind_rec` does not model.
    #
    # The allow-list names **both** offers deliberately. Without one, the default
    # set is consent-based offers only and a contract-based offer would never be
    # rendered — but a REC that discloses one lists it explicitly, which is the
    # deployment these tests describe.
    bind_rec("default")
    template_service._cache["default"] = {
        "slug": "default",
        "name": "default",
        "organization": "rec-example",
        "dataspace": {
            "organization": "rec-example",
            "organization_did": "did:web:rec.example",
            "linked_participant_did": "did:web:rec.example",
        },
        "consent": {"data_sharing": {"offers": [OFFER_CONSENT["id"], OFFER_CONTRACT["id"]]}},
    }
    monkeypatch.setattr(ms.settings, "dataspace_enabled", True)
    monkeypatch.setattr(di.settings, "dataspace_enabled", True)
    monkeypatch.setattr(di.settings, "identity_registry_url", "http://ir:30005")
    monkeypatch.setattr(di.settings, "dataspace_user_role", "DataSubject")
    monkeypatch.setattr(ms.settings, "ds_connector_url", "http://connector:8000")
    monkeypatch.setattr(ms.settings, "ds_provenance_url", "")
    di._token_provider = _mock_token_provider()


def _handler(*, resolve=None, shares=None, offers=None, prov=None, post=None):
    """One transport for the four services a member request can touch."""

    def handle(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "users/resolve" in url:
            return resolve or httpx.Response(200, json=RESOLVE_WITH_CREDENTIAL)
        if "/ns/sharing-offers" in url:
            return offers or httpx.Response(200, json=[OFFER_CONSENT, OFFER_CONTRACT])
        if "/prov/my/events" in url:
            return prov or httpx.Response(200, json=[])
        if "/consent/my/shares" in url:
            if req.method == "POST":
                return post or httpx.Response(200, json={"ok": True})
            return shares or httpx.Response(200, json=[])
        raise AssertionError(f"unexpected call to {url}")

    return handle


# ── which community, and how it is found ──────────────────────────


class TestResolveMemberRec:
    def test_the_keycloak_organization_alias_is_the_join(self, bind_rec):
        """One identifier across the platform, so no mapping table and no query."""
        bind_rec("default", organization="rec-example")

        assert ms.resolve_member_rec(_member("rec-example")) == "default"

    def test_a_member_of_nothing_resolves_to_nothing(self, bind_rec):
        bind_rec("default", organization="rec-example")

        assert ms.resolve_member_rec(_member(organization=None)) is None

    def test_an_alias_no_rec_claims_resolves_to_nothing(self, bind_rec):
        bind_rec("default", organization="rec-example")

        assert ms.resolve_member_rec(_member("some-other-org")) is None

    def test_two_communities_is_refused_not_guessed(self, bind_rec):
        """Picking one would show a member another community's allow-list.

        The offers a member may decide are their REC's, and two RECs under one
        Keycloak organization have no single answer. Loud beats plausible.
        """
        bind_rec("alpha", organization="shared-org")
        bind_rec("beta", organization="shared-org")

        with pytest.raises(ValueError, match="more than one community"):
            ms.resolve_member_rec(_member("shared-org"))


# ── the states, which used to be one state ────────────────────────


class TestTheStates:
    async def test_a_community_outside_the_dataspace_has_nothing_to_decide(
        self, monkeypatch, bind_rec
    ):
        """`no_dataspace`, and deliberately not `no_identity`.

        Collapsing them is what Phase 3 must not inherit: provisioning somebody
        whose community is not in the dataspace is worse than explaining why
        there is nothing to share.
        """
        bind_rec("default")
        template_service._cache["default"] = {
            "slug": "default",
            "name": "default",
            "organization": "rec-example",
        }
        monkeypatch.setattr(ms.settings, "dataspace_enabled", True)

        view = await ms.get_data_sharing(_member("rec-example"))

        assert view.state is ms.SharingState.NO_DATASPACE
        assert view.has_identity is False
        assert view.offers == []

    async def test_a_member_of_no_community_is_told_the_same_thing(
        self, monkeypatch, bind_rec, _dataspace
    ):
        """Different cause, same answer, and no error log.

        Being a platform user in no community this deployment serves is an
        ordinary thing to be, not a fault.
        """
        view = await ms.get_data_sharing(_member(organization=None))

        assert view.state is ms.SharingState.NO_DATASPACE

    async def test_the_whole_deployment_can_be_outside_a_dataspace(
        self, monkeypatch, bind_rec, _dataspace
    ):
        monkeypatch.setattr(ms.settings, "dataspace_enabled", False)

        view = await ms.get_data_sharing(_member())

        assert view.state is ms.SharingState.NO_DATASPACE

    async def test_a_preregistered_member_holds_no_credential(
        self, monkeypatch, bind_rec, _dataspace
    ):
        """`no_identity` — the pilot's actual state, and Phase 3's entry point."""
        _patch_httpx(
            monkeypatch,
            _handler(resolve=httpx.Response(200, json={"subject_id": "email-abc123"})),
        )

        view = await ms.get_data_sharing(_member())

        assert view.state is ms.SharingState.NO_IDENTITY
        assert view.offers == []

    async def test_a_mapping_conflict_is_terminal_and_not_a_retry(
        self, monkeypatch, bind_rec, _dataspace
    ):
        """409 from `/users/resolve`: only an operator can clear it.

        Distinct from an unreachable registry precisely so the member is not
        told to try again — retrying cannot change the state.
        """
        _patch_httpx(monkeypatch, _handler(resolve=httpx.Response(409, text="identifier email")))

        view = await ms.get_data_sharing(_member())

        assert view.state is ms.SharingState.IDENTITY_CONFLICT

    async def test_two_communities_refuses_rather_than_showing_the_wrong_offers(
        self, monkeypatch, bind_rec, _dataspace
    ):
        template_service._cache["beta"] = {
            "slug": "beta",
            "name": "beta",
            "organization": "rec-example",
        }

        view = await ms.get_data_sharing(_member("rec-example"))

        assert view.state is ms.SharingState.AMBIGUOUS_COMMUNITY

    async def test_an_unreachable_registry_is_a_failure_not_a_state(
        self, monkeypatch, bind_rec, _dataspace
    ):
        """503 is worth retrying; every `SharingState` never is."""
        _patch_httpx(monkeypatch, _handler(resolve=httpx.Response(500)))

        with pytest.raises(ms.SharingUnavailableError):
            await ms.get_data_sharing(_member())

    async def test_a_session_with_no_email_is_broken_not_identity_less(
        self, monkeypatch, bind_rec, _dataspace
    ):
        """The dataspace identity is keyed on the onboarding email.

        Without one there is nothing to resolve, which is a broken session and
        not a member who holds no credential.
        """
        with pytest.raises(ms.SharingUnavailableError, match="email"):
            await ms.get_data_sharing(_member(email=None))


# ── the merge, which moved here from the BFF ──────────────────────


class TestTheMerge:
    async def test_offers_come_through_the_recs_allow_list(self, monkeypatch, bind_rec, _dataspace):
        """The defect the move fixes.

        `../celine-webapp` read `/ns/sharing-offers` directly and rendered every
        offer the connector publishes, ignoring `consent.data_sharing.offers` —
        so a member could be shown, and grant, an offer their community does not
        publish. Resolving through `template_service` is what applies the
        allow-list, and it is why the merge belongs on this side.
        """
        template_service._cache["default"]["consent"]["data_sharing"]["offers"] = [
            OFFER_CONSENT["id"]
        ]
        _patch_httpx(monkeypatch, _handler())

        view = await ms.get_data_sharing(_member())

        assert [o["id"] for o in view.offers] == [OFFER_CONSENT["id"]]

    async def test_a_standing_decision_marks_the_offer_granted(
        self, monkeypatch, bind_rec, _dataspace
    ):
        _patch_httpx(
            monkeypatch,
            _handler(
                shares=httpx.Response(
                    200,
                    json=[
                        {
                            "offer_id": OFFER_CONSENT["id"],
                            "status": "granted",
                            "decided_at": "2026-09-01T10:00:00Z",
                            "legal_basis": {"consent_text_version": "v3"},
                        }
                    ],
                )
            ),
        )

        view = await ms.get_data_sharing(_member())
        granted = next(o for o in view.offers if o["id"] == OFFER_CONSENT["id"])

        assert granted["granted"] is True
        assert granted["decided_at"] == "2026-09-01T10:00:00Z"
        assert granted["evidence"] == {"consent_text_version": "v3"}

    async def test_a_revoked_decision_is_not_granted(self, monkeypatch, bind_rec, _dataspace):
        _patch_httpx(
            monkeypatch,
            _handler(
                shares=httpx.Response(
                    200, json=[{"offer_id": OFFER_CONSENT["id"], "status": "revoked"}]
                )
            ),
        )

        view = await ms.get_data_sharing(_member())

        assert all(o["granted"] is False for o in view.offers)

    async def test_only_a_consent_based_offer_gets_a_control(
        self, monkeypatch, bind_rec, _dataspace
    ):
        """A contract-based offer is disclosed, not toggled.

        Rendering a control for it presents a choice that does not exist, which
        is what invalidates the consent beside it.
        """
        _patch_httpx(monkeypatch, _handler())

        view = await ms.get_data_sharing(_member())
        by_id = {o["id"]: o for o in view.offers}

        assert by_id[OFFER_CONSENT["id"]]["can_decide"] is True
        assert by_id[OFFER_CONTRACT["id"]]["can_decide"] is False

    async def test_a_contract_offer_is_still_disclosed(self, monkeypatch, bind_rec, _dataspace):
        """Disclosed, not hidden: the member is told what happens without a choice."""
        _patch_httpx(monkeypatch, _handler())

        view = await ms.get_data_sharing(_member())

        assert OFFER_CONTRACT["id"] in {o["id"] for o in view.offers}

    async def test_unreachable_offers_fail_closed(self, monkeypatch, bind_rec, _dataspace):
        """As the wizard does, and for the same reason.

        A silent empty list is indistinguishable from "this community shares
        nothing", and a member shown that concludes they have nothing to
        withdraw.
        """
        _patch_httpx(monkeypatch, _handler(offers=httpx.Response(503)))

        with pytest.raises(ms.SharingUnavailableError):
            await ms.get_data_sharing(_member())


# ── deciding ──────────────────────────────────────────────────────


class TestSetDecision:
    async def test_it_posts_as_the_member_and_not_as_this_service(
        self, monkeypatch, bind_rec, _dataspace
    ):
        """The connector authenticates a data subject by credential.

        A service token here would mean this service could consent on somebody's
        behalf, which is the thing that would make the record worthless.
        """
        seen: list[httpx.Request] = []

        def post(req):
            return httpx.Response(200, json={"ok": True})

        def handle(req: httpx.Request) -> httpx.Response:
            if "/consent/my/shares" in str(req.url) and req.method == "POST":
                seen.append(req)
            return _handler()(req)

        _patch_httpx(monkeypatch, handle)
        await ms.set_data_sharing(_member(), OFFER_CONSENT["id"], enabled=True)

        assert seen[0].headers["x-subject-id"] == RESOLVE_WITH_CREDENTIAL["did"]
        assert seen[0].headers["x-user-vc"] == VC
        assert "authorization" not in seen[0].headers
        assert json.loads(seen[0].content) == {"offer_id": OFFER_CONSENT["id"], "enabled": True}

    async def test_withdrawal_is_the_same_route(self, monkeypatch, bind_rec, _dataspace):
        """Art. 7(3): as easy to withdraw as to give. The wizard can only give."""
        bodies: list[dict] = []

        def handle(req: httpx.Request) -> httpx.Response:
            if "/consent/my/shares" in str(req.url) and req.method == "POST":
                bodies.append(json.loads(req.content))
            return _handler()(req)

        _patch_httpx(monkeypatch, handle)
        await ms.set_data_sharing(_member(), OFFER_CONSENT["id"], enabled=False)

        assert bodies[0]["enabled"] is False

    async def test_a_contract_offer_is_refused_before_the_connector_sees_it(
        self, monkeypatch, bind_rec, _dataspace
    ):
        """Refused here so the message can name the offer.

        The connector answers 409 too, but from two hops away and without the
        offer's own vocabulary to explain itself.
        """
        posted: list[str] = []

        def handle(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                posted.append(str(req.url))
            return _handler()(req)

        _patch_httpx(monkeypatch, handle)

        with pytest.raises(ValueError, match="not consent-based"):
            await ms.set_data_sharing(_member(), OFFER_CONTRACT["id"], enabled=True)

        assert posted == []

    async def test_an_offer_this_rec_does_not_publish_is_refused(
        self, monkeypatch, bind_rec, _dataspace
    ):
        """Not this member's to grant. The allow-list decides both questions."""
        _patch_httpx(monkeypatch, _handler())

        with pytest.raises(ValueError, match="publishes no sharing offer"):
            await ms.set_data_sharing(_member(), "some-other-offer", enabled=True)

    async def test_a_member_with_no_identity_cannot_decide(self, monkeypatch, bind_rec, _dataspace):
        _patch_httpx(
            monkeypatch,
            _handler(resolve=httpx.Response(200, json={"subject_id": "email-abc123"})),
        )

        with pytest.raises(ms.CannotDecideError) as exc:
            await ms.set_data_sharing(_member(), OFFER_CONSENT["id"], enabled=True)

        assert exc.value.state is ms.SharingState.NO_IDENTITY


# ── history ───────────────────────────────────────────────────────


class TestHistory:
    async def test_absent_provenance_is_an_empty_list_not_a_failure(
        self, monkeypatch, bind_rec, _dataspace
    ):
        """The decisions stand without their history."""
        _patch_httpx(monkeypatch, _handler())

        state, events = await ms.get_history(_member())

        assert state is ms.SharingState.OK
        assert events == []

    async def test_it_reads_the_members_own_record(self, monkeypatch, bind_rec, _dataspace):
        monkeypatch.setattr(ms.settings, "ds_provenance_url", "http://prov:8000")
        seen: list[httpx.Request] = []

        def handle(req: httpx.Request) -> httpx.Response:
            if "/prov/my/events" in str(req.url):
                seen.append(req)
                return httpx.Response(200, json={"@graph": [{"type": "DataDisclosed"}]})
            return _handler()(req)

        _patch_httpx(monkeypatch, handle)
        state, events = await ms.get_history(_member())

        assert seen[0].headers["x-user-vc"] == VC
        assert events == [{"type": "DataDisclosed"}]

    async def test_a_broken_provenance_does_not_break_the_page(
        self, monkeypatch, bind_rec, _dataspace
    ):
        monkeypatch.setattr(ms.settings, "ds_provenance_url", "http://prov:8000")

        def handle(req: httpx.Request) -> httpx.Response:
            if "/prov/my/events" in str(req.url):
                return httpx.Response(500)
            return _handler()(req)

        _patch_httpx(monkeypatch, handle)
        state, events = await ms.get_history(_member())

        assert (state, events) == (ms.SharingState.OK, [])


# ── the credential never leaves ───────────────────────────────────


class TestNoCredentialEscapes:
    async def test_no_response_carries_the_vc(self, monkeypatch, bind_rec, _dataspace):
        """The property the move exists to provide.

        A credential that never leaves the service that resolved it cannot leak
        from the one that did not need it — but only if it also never leaves in
        a response.
        """
        _patch_httpx(monkeypatch, _handler())

        view = await ms.get_data_sharing(_member())

        assert VC not in json.dumps({"offers": view.offers})

    async def test_the_credential_redacts_its_own_repr(self):
        """It reaches log records, exception messages and test output.

        One of those eventually gets shipped somewhere.
        """
        credential = di.SubjectCredential(subject_id="did:web:x", vc_jws=VC)

        assert VC not in repr(credential)
        assert "did:web:x" in repr(credential)

    async def test_the_credential_is_selected_by_role_not_by_recency(self, monkeypatch):
        """One human holds several credentials, and the newest is often wrong.

        Somebody who is a data subject about their own consumption *and* a
        consumer user acting for an organisation holds both. Presenting the
        newest would authenticate them in a capacity they are not acting in.
        """
        monkeypatch.setattr(di.settings, "dataspace_user_role", "DataSubject")
        _patch_httpx(
            monkeypatch,
            lambda req: httpx.Response(
                200,
                json={
                    "subject_id": "s",
                    "did": "did:web:x",
                    "role": "ConsumerUser",
                    "vc_jws": "newest-and-wrong",
                    "credentials": [
                        {"role": "ConsumerUser", "vc_jws": "newest-and-wrong"},
                        {"role": "DataSubject", "vc_jws": VC},
                    ],
                },
            ),
        )
        access = di.RegistryAccess(base_url="http://ir", headers={})

        credential = await di.resolve_subject_credential(access, email="a@example.org")

        assert credential is not None
        assert credential.vc_jws == VC


# ── the routes ────────────────────────────────────────────────────


class TestTheRoutes:
    @pytest.fixture()
    def client(self, issue_token):
        """The real app, so mount order is exercised rather than assumed.

        `issue_token` is requested for its side effect: it points OIDC at the
        test issuer, so `get_current_user` verifies against a key this test owns
        instead of whatever the ambient environment configures.
        """
        from fastapi.testclient import TestClient

        from celine.onboarding.main import create_app

        return TestClient(create_app(), raise_server_exceptions=False)

    def test_the_member_router_is_not_shadowed_by_rec_slug(self, client):
        """The mount-order trap, pinned.

        `main.py` mounts seven routers at `/api/{rec_slug}`, and `me` matches it.
        A `/api/me` router added at the end — beside the admin router, which is
        last for the opposite reason — would be shadowed, and this route would
        404 as a REC that does not exist rather than 401 as a route needing a
        token. The two status codes are the whole assertion.
        """
        assert client.get("/api/me/data-sharing").status_code == 401

    def test_every_member_route_needs_a_token(self, client):
        """No capability, but emphatically not no authentication.

        `/api/me` is not behind `AdminAuthMiddleware` — `is_admin_path` matches
        only `/api/admin` — so the dependency is the only thing standing here.
        """
        assert client.get("/api/me/data-sharing").status_code == 401
        assert client.get("/api/me/data-sharing/history").status_code == 401
        assert (
            client.post("/api/me/data-sharing/some-offer", json={"enabled": True}).status_code
            == 401
        )

    def test_a_member_needs_no_group_and_no_capability(
        self, client, issue_token, monkeypatch, bind_rec, _dataspace
    ):
        """A member holds no console group, and must still reach their own consent.

        This is the property that separates this surface from every other
        authenticated route in the service: an operator capability here would
        mean somebody else could decide, which is what makes a consent record
        worth nothing.
        """
        _patch_httpx(monkeypatch, _handler())
        token = issue_token(
            sub="member-sub",
            email="member@example.org",
            organization={"rec-example": {"id": "org-uuid", "groups": []}},
        )

        resp = client.get("/api/me/data-sharing", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "ok"
        assert body["has_identity"] is True
        assert {o["id"] for o in body["offers"]} == {OFFER_CONSENT["id"], OFFER_CONTRACT["id"]}
        assert VC not in resp.text

    def test_the_state_reaches_the_caller(
        self, client, issue_token, monkeypatch, bind_rec, _dataspace
    ):
        """`has_identity: false` alone is what the BFF could already say.

        `state` is the half that lets the page say *why*, which is the whole
        point: "your community does not take part" and "you have no credential
        yet" need different sentences and one of them is Phase 3's job to fix.
        """
        _patch_httpx(
            monkeypatch,
            _handler(resolve=httpx.Response(200, json={"subject_id": "email-abc123"})),
        )
        token = issue_token(
            sub="member-sub",
            email="member@example.org",
            organization={"rec-example": {"id": "org-uuid", "groups": []}},
        )

        body = client.get(
            "/api/me/data-sharing", headers={"Authorization": f"Bearer {token}"}
        ).json()

        assert body == {"has_identity": False, "state": "no_identity", "offers": []}

    def test_deciding_something_undecidable_is_409(
        self, client, issue_token, monkeypatch, bind_rec, _dataspace
    ):
        """409, not 403: nothing is wrong with this member's authorisation.

        They are in a state where the decision does not exist to be made.
        """
        _patch_httpx(monkeypatch, _handler())
        token = issue_token(
            sub="member-sub",
            email="member@example.org",
            organization={"rec-example": {"id": "org-uuid", "groups": []}},
        )

        resp = client.post(
            f"/api/me/data-sharing/{OFFER_CONTRACT['id']}",
            json={"enabled": True},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 409
        assert "not consent-based" in resp.json()["detail"]
