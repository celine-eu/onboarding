"""The POD-list export through the running app, against connectors it has to route between.

ADR-0008, one tier above `tests/test_pod_list_routing.py`: a real app booted as a
subprocess, a real Postgres holding the community and its members, a real signed
operator token, and the export pressed through `POST /api/admin/{rec}/exports/pod-list`.
Only the dataspace is a stand-in — one small HTTP server playing the community's
connector, the grid operator's connector, the identity registry and the offers
vocabulary, and recording what it was asked.

**Its own database**, created beside the suite's and dropped afterwards. The
community here declares data sharing, and the app refuses to boot a community
like that without a dataspace configured — which is exactly how the suite's own
app runs. Seeding it into the shared database would break every other test's
app on its next start.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import os
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from .conftest import E2E_DATABASE_URL, REPO_ROOT, _free_port

pytestmark = pytest.mark.e2e

REC = "e2e-routed"
COMMUNITY = "example-rec"
DSO = "example-dso"
RECIPIENT = "example-recipient"
RECIPIENT_DID = "did:web:recipient.example.org"

RELEASE = "meter-release"  # held only at the grid operator's connector
RESEARCH = "research"  # held at both connectors
INCENTIVE = "incentive"  # the community's own, several datasets that disagree
FLEX = "flex"  # the community's own; one member granted, one withdrew

ALICE = "did:web:users.example.org:alice"
BOB = "did:web:users.example.org:bob"
PODS = {ALICE: "IT001E00000001", BOB: "IT001E00000002"}


def _offer(offer_id: str) -> dict:
    return {
        "id": offer_id,
        "purpose": "Research",
        "requires_consent": True,
        "recipients": {"recipient": RECIPIENT, "recipient_role": "operations"},
        "consent_text_version": "1.0",
    }


def _datasets(*pairs: tuple[str, set[str]]) -> list[dict]:
    return [
        {"dataset_id": d, "subject_ids": sorted(ids), "subject_count": len(ids)} for d, ids in pairs
    ]


#: What each connector holds, by (connector, offer). Absent is a 422: that
#: connector holds no dataset for the offer.
HOLDS = {
    ("dso", RELEASE): _datasets(("dso_readings_15m", {ALICE, BOB})),
    ("own", RESEARCH): _datasets(("rec_meters", {ALICE})),
    ("dso", RESEARCH): _datasets(("dso_readings", {ALICE})),
    ("own", INCENTIVE): _datasets(("settlement_15m", {ALICE, BOB}), ("commitment", {ALICE})),
    ("own", FLEX): _datasets(("flex_15m", {ALICE})),
}

WITHDRAWN_AT = "2026-09-20T08:30:00Z"

#: `GET /consent/admin/decisions`, by (connector, offer): subject → decisions, or a
#: status. Unset for an offer the connector holds: no subject decided.
DECISIONS: dict[tuple[str, str], dict | int] = {
    # An older connector: no decisions route at all.
    ("dso", RELEASE): 404,
    ("own", FLEX): {
        ALICE: [{"dataset_id": "flex_15m", "state": "granted", "decided_by": "subject"}],
        BOB: [
            {
                "dataset_id": "flex_15m",
                "state": "withdrawn",
                "decided_by": "subject",
                "revoked_at": WITHDRAWN_AT,
            }
        ],
    },
}

ORG_CLIENT = f"svc-ds-connector-{COMMUNITY}"


def _client_of(authorization: str) -> str:
    """Which client a bearer token was minted for — the stub's only question about it."""
    payload = authorization.split(" ", 1)[1].split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["azp"]


class _Dataspace:
    """The stand-in, recording every request as (connector-or-service, method, path)."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str]] = []
        #: (connector, path, client) for every connector read, so a test can say
        #: which credential each route was asked with.
        self.clients: list[tuple[str, str, str]] = []
        state = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status: int, body) -> None:
                raw = (body if isinstance(body, str) else json.dumps(body)).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802 - http.server's interface
                url = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                if url.path == "/ns/sharing-offers":
                    return self._send(
                        200, [_offer(o) for o in (RELEASE, RESEARCH, INCENTIVE, FLEX)]
                    )
                if url.path == "/owners/resolve":
                    return self._send(
                        200, {"id": RECIPIENT, "did": RECIPIENT_DID, "status": "verified"}
                    )
                where, _, rest = url.path.lstrip("/").partition("/")
                state.requests.append((where, "GET", "/" + rest))
                state.clients.append(
                    (where, "/" + rest, _client_of(self.headers.get("Authorization", "")))
                )
                if rest == "consent/admin/decisions":
                    held = HOLDS.get((where, query["offer_id"]))
                    decided = DECISIONS.get((where, query["offer_id"]), {})
                    if isinstance(decided, int):
                        return self._send(decided, "Not Found")
                    if held is None:
                        return self._send(422, "offer resolves to no dataset")
                    return self._send(
                        200,
                        {
                            "offer_id": query["offer_id"],
                            "datasets": [d["dataset_id"] for d in held],
                            "limit": int(query.get("limit", 50)),
                            "subjects": [
                                {"subject_id": s, "decisions": decided[s]} for s in sorted(decided)
                            ],
                            "next_cursor": None,
                        },
                    )
                if rest == "consent/admin/shares":
                    held = HOLDS.get((where, query["offer_id"]))
                    if held is None:
                        return self._send(422, "offer resolves to no dataset")
                    return self._send(200, {"offer_id": query["offer_id"], "datasets": held})
                return self._send(404, "not found")

            def do_POST(self):  # noqa: N802 - http.server's interface
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                where, _, rest = urlsplit(self.path).path.lstrip("/").partition("/")
                state.requests.append((where, "POST", "/" + rest))
                held = HOLDS.get((where, body.get("offer_id")))
                if rest != "admin/disclosure" or held is None:
                    return self._send(422, "offer resolves to no dataset")
                return self._send(
                    200,
                    {
                        "status": "recorded",
                        "offer_id": body["offer_id"],
                        "disclosures": [
                            {"dataset_id": d["dataset_id"], "consent_snapshot_hash": "a" * 64}
                            for d in held
                        ],
                    },
                )

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def asked(self, where: str, method: str = "GET") -> list[str]:
        return [path for w, m, path in self.requests if w == where and m == method]

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture(scope="module")
def dataspace():
    server = _Dataspace()
    yield server
    server.stop()


def _server_url(database: str) -> str:
    from sqlalchemy.engine import make_url

    return make_url(E2E_DATABASE_URL).set(database=database).render_as_string(hide_password=False)


@pytest.fixture(scope="module")
def database(_guard, dataspace):
    """A database of its own, migrated and seeded with the community and two members."""
    import asyncpg
    from sqlalchemy.engine import make_url

    base = make_url(E2E_DATABASE_URL)
    name = f"{base.database}_routing_{uuid.uuid4().hex[:8]}"
    admin = dict(
        user=base.username,
        password=base.password,
        host=base.host,
        port=base.port,
        database="postgres",
    )

    async def _admin(sql: str) -> None:
        conn = await asyncpg.connect(**admin)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    url = _server_url(name)
    subprocess.run(
        ["uv", "run", "--project", "src", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": url},
        check=True,
        capture_output=True,
    )

    async def _seed() -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from celine.onboarding.models.rec import Rec
        from celine.onboarding.models.submission import Submission, SubmissionStatus

        engine = create_async_engine(url)
        try:
            async with async_sessionmaker(engine)() as db:
                db.add(
                    Rec(
                        slug=REC,
                        name="E2E Routed Community",
                        active=True,
                        manifest={
                            "slug": REC,
                            "name": "E2E Routed Community",
                            "organization": COMMUNITY,
                            "steps": ["consents", "personal", "review"],
                            "consent": {"data_sharing": {}},
                            "dataspace": {
                                "organization": COMMUNITY,
                                "connectors": [
                                    {
                                        "holder": DSO,
                                        "url": f"{dataspace.url}/dso",
                                        "offers": [RELEASE, RESEARCH],
                                    },
                                    {"holder": COMMUNITY, "offers": [RESEARCH]},
                                ],
                            },
                        },
                    )
                )
                await db.flush()
                for did, pod in PODS.items():
                    db.add(
                        Submission(
                            rec_slug=REC,
                            status=SubmissionStatus.APPROVED,
                            session_token=uuid.uuid4().hex,
                            consent_ip="127.0.0.1",
                            pod_code=pod,
                            dataspace_did=did,
                        )
                    )
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(_seed())
    yield url
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture(scope="module")
def routed_api(idp, database, dataspace) -> str:
    """The app, with a dataspace: the community's connector, and a registry and
    vocabulary to resolve the offer's recipient from. No rec registry, so the
    supply points come from the members' submissions."""
    import time

    port = _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": database,
        "OIDC_BASE_URL": idp.issuer,
        "OIDC_JWKS_URI": idp.jwks_uri,
        "REQUIRE_ENCRYPTION": "false",
        "EXTRACTION_ENABLED": "false",
        "LLM_BASE_URL": "",
        "DPA_SIGNED": "",
        "OPENAI_API_KEY": "",
        "DPA_SMS_SIGNED": "yes",
        "ADMIN_TOKEN": "",
        "DS_NS_URL": dataspace.url,
        "DS_CONNECTOR_URL": f"{dataspace.url}/own",
        "IDENTITY_REGISTRY_URL": dataspace.url,
        "DS_ONBOARDING_CLIENT_ID": "svc-ds-onboarding",
        "DS_ONBOARDING_CLIENT_SECRET": "e2e",
        # The community's own client, which the decisions list is read with.
        "DS_ORG_CLIENT_SECRET": "e2e",
        "REC_REGISTRY_URL": "",
        "DATASPACE_ENABLED": "false",
        "POLICIES_DIR": str(REPO_ROOT / "policies"),
        "TEMPLATES_DIR": str(REPO_ROOT / "templates"),
    }
    process = subprocess.Popen(
        [
            "uv",
            "run",
            "--project",
            "src",
            "uvicorn",
            "celine.onboarding.main:app",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    for _ in range(120):
        if process.poll() is not None:
            raise RuntimeError(f"API exited early:\n{process.stdout.read()}")
        try:
            if httpx.get(f"{base_url}/api/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.5)
    else:
        process.terminate()
        raise RuntimeError("API did not come up")

    yield base_url

    process.terminate()
    process.wait(timeout=15)


def _press(routed_api: str, idp, offer_id: str, **extra) -> httpx.Response:
    return httpx.post(
        f"{routed_api}/api/admin/{REC}/exports/pod-list",
        headers={"Authorization": f"Bearer {idp.operator(COMMUNITY, 'managers')}"},
        json={"offer_id": offer_id, **extra},
        timeout=30,
    )


def test_an_offer_held_only_by_another_participant_exports_from_its_holder(
    routed_api, idp, dataspace
):
    dataspace.requests.clear()

    response = _press(routed_api, idp, RELEASE)

    assert response.status_code == 200, response.text
    assert PODS[ALICE] in response.text and PODS[BOB] in response.text
    assert f"#   dso_readings_15m, held by {DSO}" in response.text
    assert dataspace.asked("own") == [] and dataspace.asked("own", "POST") == []
    assert dataspace.asked("dso", "POST") == []
    # That connector serves no decisions list, and the file says so by name.
    assert f"# Withdrawals NOT reported for {DSO}'s connector" in response.text


def test_an_offer_held_at_two_connectors_is_read_at_both(routed_api, idp, dataspace):
    dataspace.requests.clear()

    response = _press(routed_api, idp, RESEARCH)

    assert response.status_code == 200, response.text
    assert PODS[ALICE] in response.text and PODS[BOB] not in response.text
    assert f"#   rec_meters, held by {COMMUNITY}" in response.text
    assert f"#   dso_readings, held by {DSO}" in response.text
    assert dataspace.asked("own", "POST") == [] and dataspace.asked("dso", "POST") == []


def test_datasets_that_disagree_are_refused_and_nothing_is_recorded(routed_api, idp, dataspace):
    dataspace.requests.clear()

    response = _press(routed_api, idp, INCENTIVE)

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "do not agree" in detail
    assert "2 subjects: settlement_15m" in detail and "1 subject: commitment" in detail
    assert dataspace.asked("own", "POST") == [] and dataspace.asked("dso", "POST") == []


def test_a_member_who_withdrew_is_reported_as_withdrawn(routed_api, idp, dataspace):
    """ADR-0009: withdrawn, in its own column, never under the authorised one — and
    read as the community, with its organisation client, where the audience read
    uses this service's own."""
    dataspace.requests.clear()
    dataspace.clients.clear()

    response = _press(routed_api, idp, FLEX)

    assert response.status_code == 200, response.text
    body = "\n".join(ln for ln in response.text.splitlines() if not ln.startswith("#"))
    rows = list(csv.DictReader(io.StringIO(body)))
    assert [r["authorised_pod_code"] for r in rows if r["authorised_pod_code"]] == [PODS[ALICE]]
    assert [
        (r["withdrawn_pod_code"], r["withdrawn_at"], r["withdrawn_by"])
        for r in rows
        if r["withdrawn_pod_code"]
    ] == [(PODS[BOB], WITHDRAWN_AT, "subject")]
    assert ("own", "/consent/admin/decisions", ORG_CLIENT) in dataspace.clients
    assert ("own", "/consent/admin/shares", "svc-ds-onboarding") in dataspace.clients


def test_the_export_is_evidence_and_posts_nothing(routed_api, idp, dataspace):
    """ADR-0010: the file is the community's own record. Nothing is posted to any
    connector — no `DataDisclosed` — and the header says what the file is."""
    dataspace.requests.clear()

    response = _press(routed_api, idp, FLEX)

    assert response.status_code == 200, response.text
    posted = [(w, p) for w, m, p in dataspace.requests if m == "POST"]
    assert posted == [], f"the export posted {posted}"
    assert response.text.startswith("# Evidence: which supply points stood authorised")


def test_an_old_body_naming_a_recipient_is_still_answered(routed_api, idp, dataspace):
    """`recipient_ref` is gone from the contract; a caller still sending it — naming
    anybody at all — gets the same evidence, because unknown fields are ignored."""
    response = _press(routed_api, idp, FLEX, recipient_ref="someone-else")

    assert response.status_code == 200, response.text
    assert f"({RECIPIENT_DID})" in response.text
