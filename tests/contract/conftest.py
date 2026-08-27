"""Configuration for the ds contract checks.

Two halves, and they catch different things:

- `test_ds_openapi_contract.py` reads what ds *publishes* and proves every call
  in `inventory.py` still exists with the fields we send.
- `test_ds_semantics.py` calls ds and proves the things a schema cannot say.

**Both skip rather than fail when ds is not reachable, and both say so loudly.**
A check that silently stops running is how the drift this plan repairs survived
three weeks — so the skip names the URL it could not reach, and the task runs
pytest with `-rs` so the reasons are printed rather than counted.
"""
from __future__ import annotations

import os

import httpx
import pytest

IR_URL = os.environ.get("DS_CONTRACT_IR_URL", "http://127.0.0.1:30005")
CONNECTOR_URL = os.environ.get(
    "DS_CONTRACT_CONNECTOR_URL", "http://portal.dataspaces.localhost/api/connector"
)
PROVENANCE_URL = os.environ.get(
    "DS_CONTRACT_PROVENANCE_URL", "http://portal.dataspaces.localhost/api/provenance"
)
TOKEN_URL = os.environ.get(
    "DS_CONTRACT_TOKEN_URL",
    "http://keycloak.dataspaces.localhost/realms/dataspaces/protocol/openid-connect/token",
)
CLIENT_ID = os.environ.get("DS_CONTRACT_CLIENT_ID", "svc-ds-onboarding")
CLIENT_SECRET = os.environ.get("DS_CONTRACT_CLIENT_SECRET", "svc-ds-onboarding")

BASES = {"ir": IR_URL, "connector": CONNECTOR_URL, "provenance": PROVENANCE_URL}


def _unreachable(what: str, url: str, exc: Exception) -> str:
    return (
        f"\n  DS CONTRACT CHECK DID NOT RUN — {what} is not reachable at {url}\n"
        f"  ({type(exc).__name__}: {exc})\n"
        f"  This is a skip, not a pass. Start the ds stack, or point\n"
        f"  DS_CONTRACT_*_URL at one, and run again.\n"
    )


@pytest.fixture(scope="session")
def specs() -> dict[str, dict]:
    """The OpenAPI document each ds service publishes.

    Fetched without credentials on purpose: all three serve their spec
    unauthenticated, so this half of the check needs reachability and nothing
    else — no client, no secret, no realm.
    """
    out: dict[str, dict] = {}
    for name, base in BASES.items():
        url = f"{base.rstrip('/')}/openapi.json"
        try:
            resp = httpx.get(url, timeout=10)
            resp.raise_for_status()
            out[name] = resp.json()
        except Exception as exc:  # noqa: BLE001 — any failure is "not reachable"
            pytest.skip(_unreachable(f"ds {name}", url, exc), allow_module_level=True)
    return out


@pytest.fixture(scope="session")
def token() -> str:
    """An `svc-ds-onboarding` access token — the identity this service really uses.

    Checking with a broader client would prove the endpoint exists and not that
    *we* may call it, which is half of what went wrong: `/owners/resolve` was
    reachable all along, just not by us.
    """
    try:
        resp = httpx.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]
    except Exception as exc:  # noqa: BLE001
        pytest.skip(_unreachable("the ds token endpoint", TOKEN_URL, exc),
                    allow_module_level=True)


@pytest.fixture(scope="session")
def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
