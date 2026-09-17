"""Configuration for the ds contract checks.

Two halves, and they catch different things:

- `test_ds_openapi_contract.py` reads what ds *publishes* and proves every call
  in `inventory.py` still exists with the fields we send.
- `test_ds_semantics.py` calls ds and proves the things a schema cannot say.

**Both skip rather than fail when the suite is not pointed at a ds, and both say
so loudly.** A check that silently stops running is how three weeks of drift
survived unseen — so the skip names what it could not reach, or what was never
configured, and the task runs pytest with `-rs` so the reasons are printed
rather than counted.

**Nothing below has a default, and that is deliberate.** This service does not
require a dataspace: the integration is off unless configured, and every ds
address is empty until someone fills it in. A default here would have to name
one particular deployment's hosts, credentials and seeded data — telling every
reader that some stack they cannot see is the one this service means, and
checking the suite against it while reporting green. Absent is the only answer
that is true on every checkout. `task test:contract` lists the variables.
"""

from __future__ import annotations

import os

import httpx
import pytest


def _env(name: str) -> str | None:
    """The variable, or None when it is unset or blank.

    Blank counts as unset so that an environment file carrying an empty
    assignment skips the suite rather than sending requests to `""`.
    """
    return os.environ.get(name, "").strip() or None


IR_URL = _env("DS_CONTRACT_IR_URL")
CONNECTOR_URL = _env("DS_CONTRACT_CONNECTOR_URL")
PROVENANCE_URL = _env("DS_CONTRACT_PROVENANCE_URL")
TOKEN_URL = _env("DS_CONTRACT_TOKEN_URL")
CLIENT_ID = _env("DS_CONTRACT_CLIENT_ID")
CLIENT_SECRET = _env("DS_CONTRACT_CLIENT_SECRET")

#: The seeded data the semantic checks assert against. These name one
#: deployment's fixtures as precisely as the URLs name its hosts — an owner
#: whose id and alias differ, the offer published under each legal basis — so
#: they are supplied the same way and absent for the same reason.
OWNER_ID = _env("DS_CONTRACT_OWNER_ID")
OWNER_ALIAS = _env("DS_CONTRACT_OWNER_ALIAS")
CONSENT_OFFER = _env("DS_CONTRACT_CONSENT_OFFER")
CONTRACT_OFFER = _env("DS_CONTRACT_CONTRACT_OFFER")
#: A deployment may publish **no** contract-based offer, and says so with the
#: literal `none` — distinct from unset, which still skips as unconfigured. The
#: checks that need such an offer are then *deselected*, not skipped, and
#: `test_a_deployment_declaring_no_contract_offer_publishes_none` checks the claim
#: itself, so the declaration cannot hide an offer that is really there.
NO_CONTRACT_OFFER = CONTRACT_OFFER == "none"
#: A subject DID under the deployment's own participant, used only in requests
#: that must be refused before anything is created.
PROBE_SUBJECT = _env("DS_CONTRACT_PROBE_SUBJECT")

ADDRESSES = {
    "DS_CONTRACT_IR_URL": IR_URL,
    "DS_CONTRACT_CONNECTOR_URL": CONNECTOR_URL,
    "DS_CONTRACT_PROVENANCE_URL": PROVENANCE_URL,
}
CREDENTIALS = {
    "DS_CONTRACT_TOKEN_URL": TOKEN_URL,
    "DS_CONTRACT_CLIENT_ID": CLIENT_ID,
    "DS_CONTRACT_CLIENT_SECRET": CLIENT_SECRET,
}
FIXTURE_IDS = {
    "DS_CONTRACT_OWNER_ID": OWNER_ID,
    "DS_CONTRACT_OWNER_ALIAS": OWNER_ALIAS,
    "DS_CONTRACT_CONSENT_OFFER": CONSENT_OFFER,
    "DS_CONTRACT_CONTRACT_OFFER": CONTRACT_OFFER,
    "DS_CONTRACT_PROBE_SUBJECT": PROBE_SUBJECT,
}

BASES = {"ir": IR_URL, "connector": CONNECTOR_URL, "provenance": PROVENANCE_URL}


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "needs_contract_offer: needs the deployment to publish a contract-based offer",
    )
    config.addinivalue_line(
        "markers",
        "declares_no_contract_offer: runs only where DS_CONTRACT_CONTRACT_OFFER=none",
    )


def pytest_collection_modifyitems(config, items):
    """Deselect whichever contract-offer checks do not apply to this deployment.

    Deselected rather than skipped because a skip means "could not check" and a
    deployment with no contract offer is a checked fact, asserted by its own test.
    The count still shows in pytest's summary line.
    """
    # Exactly one of the two sets applies to a deployment, so neither ever skips.
    unwanted = "needs_contract_offer" if NO_CONTRACT_OFFER else "declares_no_contract_offer"
    kept, dropped = [], []
    for item in items:
        (dropped if item.get_closest_marker(unwanted) else kept).append(item)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept


def skip_unconfigured(
    names: dict[str, str | None], what: str, *, module_level: bool = False
) -> None:
    """Skip, naming every variable that is missing rather than the first one.

    One run should tell somebody everything they have to set. Reporting them one
    at a time turns pointing the suite at a deployment into a guessing game, and
    a guessing game is what gets a check switched off.
    """
    absent = sorted(name for name, value in names.items() if not value)
    if not absent:
        return
    pytest.skip(
        f"\n  DS CONTRACT CHECK DID NOT RUN — {what} is not configured\n"
        f"  unset: {', '.join(absent)}\n"
        f"  This is a skip, not a pass. Point the suite at a ds deployment\n"
        f"  and run again; `task test:contract` lists every variable.\n",
        allow_module_level=module_level,
    )


def _unreachable(what: str, url: str, exc: Exception) -> str:
    return (
        f"\n  DS CONTRACT CHECK DID NOT RUN — {what} is not reachable at {url}\n"
        f"  ({type(exc).__name__}: {exc})\n"
        f"  This is a skip, not a pass. Start the ds deployment this suite is\n"
        f"  pointed at, or point DS_CONTRACT_*_URL at a running one.\n"
    )


@pytest.fixture(scope="session")
def specs() -> dict[str, dict]:
    """The OpenAPI document each ds service publishes.

    Fetched without credentials on purpose: all three serve their spec
    unauthenticated, so this half of the check needs reachability and nothing
    else — no client, no secret, no realm.
    """
    skip_unconfigured(ADDRESSES, "the ds deployment to check against", module_level=True)

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
    """An access token for the client this service really authenticates as.

    Checking with a broader client would prove the endpoint exists and not that
    *we* may call it, which is half of what went wrong: `/owners/resolve` was
    reachable all along, just not by us.
    """
    skip_unconfigured(CREDENTIALS, "the client this suite authenticates as", module_level=True)

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
        pytest.skip(_unreachable("the ds token endpoint", TOKEN_URL, exc), allow_module_level=True)


@pytest.fixture(scope="session")
def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
