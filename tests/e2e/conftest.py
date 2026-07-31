"""End-to-end fixtures: a real app, a real database, real signed tokens.

Separate from the unit suite, which is deliberately database-free. These boot
uvicorn as a subprocess and talk to it over HTTP, so what is under test is the
whole path — middleware, token verification, policy evaluation, the service layer,
Postgres — rather than a function called directly.

Skipped unless `ONBOARDING_E2E_DATABASE_URL` points at a database that can be
migrated. Nothing here runs against a database somebody cares about: it truncates.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from idp import TestIdp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_DATABASE_URL = os.environ.get("ONBOARDING_E2E_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not E2E_DATABASE_URL, reason="set ONBOARDING_E2E_DATABASE_URL to run e2e tests"
)

ORG = "community-a"
OTHER_ORG = "community-b"
REC = "e2e-rec"
OTHER_REC = "e2e-other"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def _guard():
    if not E2E_DATABASE_URL:
        pytest.skip("ONBOARDING_E2E_DATABASE_URL is not set")


@pytest.fixture(scope="session")
def idp(_guard) -> TestIdp:
    server = TestIdp().start()
    yield server
    server.stop()


@pytest.fixture(scope="session")
def _migrated(_guard):
    subprocess.run(
        ["uv", "run", "--project", "src", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": E2E_DATABASE_URL},
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
def _seeded(_migrated):
    """Two communities in two organizations — the tenancy boundary needs both."""
    import asyncio

    async def seed():
        os.environ["DATABASE_URL"] = E2E_DATABASE_URL
        from sqlalchemy import delete, select

        from celine.onboarding.models.database import async_session
        from celine.onboarding.models.rec import Rec

        async with async_session() as db:
            for slug, org, name in (
                (REC, ORG, "E2E Community"),
                (OTHER_REC, OTHER_ORG, "E2E Other Community"),
            ):
                existing = (
                    await db.execute(select(Rec).where(Rec.slug == slug))
                ).scalar_one_or_none()
                manifest = {
                    "slug": slug,
                    "name": name,
                    "organization": org,
                    # No phone_verify: the gate is tested in the unit suite, and
                    # here it would only stand between every test and approval.
                    "steps": ["consents", "personal", "review"],
                }
                if existing:
                    existing.manifest = manifest
                    existing.name = name
                else:
                    db.add(Rec(slug=slug, name=name, manifest=manifest, active=True))
            await db.commit()

        # Leave no submissions behind from a previous run: several assertions
        # count rows.
        async with async_session() as db:
            from celine.onboarding.models.submission import Submission

            await db.execute(delete(Submission).where(Submission.rec_slug.in_([REC, OTHER_REC])))
            await db.commit()

    asyncio.run(seed())


@pytest.fixture(scope="session")
def api(idp: TestIdp, _seeded) -> str:
    """A live app, configured to trust the test issuer. Returns its base URL."""
    port = _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": E2E_DATABASE_URL,
        "OIDC_BASE_URL": idp.issuer,
        "OIDC_JWKS_URI": idp.jwks_uri,
        "REQUIRE_ENCRYPTION": "false",
        "DPA_SIGNED": "yes",
        "DPA_SMS_SIGNED": "yes",
        "ADMIN_TOKEN": "",
        "DS_NS_URL": "",
        "DS_CONNECTOR_URL": "",
        "REC_REGISTRY_URL": "",
        "DATASPACE_ENABLED": "false",
        "DATASPACE_KEYCLOAK_ENABLED": "false",
        "POLICIES_DIR": str(REPO_ROOT / "policies"),
        "TEMPLATES_DIR": str(REPO_ROOT / "templates"),
        # Every request comes from 127.0.0.1, so the per-IP limits would
        # otherwise stop the suite a third of the way in.
        "RATE_LIMIT_SUBMISSIONS": "10000/hour",
        "RATE_LIMIT_PDF": "10000/minute",
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


@pytest.fixture()
def client(api: str) -> httpx.Client:
    with httpx.Client(base_url=api, timeout=30) as c:
        yield c


@pytest.fixture()
def submission(client: httpx.Client, idp: TestIdp):
    """A submission in `under_review`, ready to be approved.

    Created through the *public* wizard endpoints, so the fixture also asserts
    that the anonymous path still works with the console in place.
    """

    def _make(rec: str = REC) -> dict:
        created = client.post(
            f"/api/{rec}/submissions",
            json={
                "gdpr_consent": True,
                "policy_consent": True,
                "statute_consent": True,
                "gdpr_consent_version": "1.0",
                "policy_consent_version": "1.0",
                "statute_consent_version": "1.0",
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()

        session = {"X-Session-Token": body["session_token"]}
        patched = client.patch(
            f"/api/{rec}/submissions/{body['id']}",
            headers=session,
            json={
                "first_name": "Mario",
                "last_name": "Rossi",
                "email": f"mario-{uuid.uuid4().hex[:8]}@example.org",
                "fiscal_code": "RSSMRA85T10A562S",
                "pod_code": "IT001E12345678",
            },
        )
        assert patched.status_code == 200, patched.text

        admin = {
            "Authorization": f"Bearer {idp.operator(ORG if rec == REC else OTHER_ORG, 'admins')}"
        }
        for target in ("submitted", "under_review"):
            moved = client.post(
                f"/api/admin/{rec}/submissions/{body['id']}/transition",
                headers=admin,
                json={"target": target},
            )
            assert moved.status_code == 200, moved.text
        return client.get(f"/api/admin/{rec}/submissions/{body['id']}", headers=admin).json()

    return _make
