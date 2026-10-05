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

#: The community's **organisation** client — `svc-ds-connector-<alias>` — which
#: is the caller ds requires for a consent registration now that it has withdrawn
#: the plain-service path. Deliberately separate from `DS_CONTRACT_CLIENT_*`:
#: those name the service client this checkout authenticates as everywhere else,
#: and the point of the checks that use this one is that the two are *not*
#: interchangeable. Supplied by the deployment, absent by default, and its own
#: skip group so a deployment that has not configured it still runs the rest.
ORG_CLIENT_ID = _env("DS_CONTRACT_ORG_CLIENT_ID")
ORG_CLIENT_SECRET = _env("DS_CONTRACT_ORG_CLIENT_SECRET")

#: The community's **collector** client — `svc-ds-collector-<alias>` — which every
#: act this service performs for a community is made as on a ds that serves
#: collector clients (ds collector contract v1): memberships, credentials, the
#: consent write and read-backs, the audience read. One token **per scope**.
#: Unset, the checks fall back to what this service does in its development
#: transition — the organisation (connector) client for the consent write and
#: read-backs, the service client for the audience read — and the checks only a
#: collector can make are **deselected**, loudly, like the holder's.
COLLECTOR_CLIENT_ID = _env("DS_CONTRACT_COLLECTOR_CLIENT_ID")
COLLECTOR_CLIENT_SECRET = _env("DS_CONTRACT_COLLECTOR_CLIENT_SECRET")
COLLECTOR = {
    "DS_CONTRACT_COLLECTOR_CLIENT_ID": COLLECTOR_CLIENT_ID,
    "DS_CONTRACT_COLLECTOR_CLIENT_SECRET": COLLECTOR_CLIENT_SECRET,
}
COLLECTOR_UNSET = not all(COLLECTOR.values())

#: The scopes, as ds names them. Kept literal here rather than imported from the
#: service: this suite checks ds, and a rename on our side must not move it.
CONSENT_PROVISION = "connector.consent.provision"
CONSENT_COLLECTOR_READ = "connector.consent.collector.read"
CONSENT_AUDIENCE = "connector.consent.audience"

#: **Another participant's connector**, and an offer whose data it holds and the
#: community's own connector (`DS_CONTRACT_CONNECTOR_URL`) does not — the case
#: the POD export has to route (ADR-0008). Read-only checks. Unlike every other
#: group these are **deselected**, not skipped, while both are unset, and the
#: summary says so: a deployment that runs this suite and treats any skip as a
#: failure keeps passing until it names a holder, and is told that it has not.
HOLDER_CONNECTOR_URL = _env("DS_CONTRACT_HOLDER_CONNECTOR_URL")
HOLDER_OFFER = _env("DS_CONTRACT_HOLDER_OFFER")
HOLDER = {
    "DS_CONTRACT_HOLDER_CONNECTOR_URL": HOLDER_CONNECTOR_URL,
    "DS_CONTRACT_HOLDER_OFFER": HOLDER_OFFER,
}
HOLDER_UNSET = not any(HOLDER.values())

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
ORGANISATION_CREDENTIALS = {
    "DS_CONTRACT_TOKEN_URL": TOKEN_URL,
    "DS_CONTRACT_ORG_CLIENT_ID": ORG_CLIENT_ID,
    "DS_CONTRACT_ORG_CLIENT_SECRET": ORG_CLIENT_SECRET,
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
    config.addinivalue_line(
        "markers",
        "needs_holder: needs DS_CONTRACT_HOLDER_* — deselected, loudly, while both are unset",
    )
    config.addinivalue_line(
        "markers",
        "needs_collector: needs DS_CONTRACT_COLLECTOR_CLIENT_* — deselected, loudly, while unset",
    )


def pytest_collection_modifyitems(config, items):
    """Deselect whichever contract-offer checks do not apply to this deployment.

    Deselected rather than skipped because a skip means "could not check" and a
    deployment with no contract offer is a checked fact, asserted by its own test.
    The count still shows in pytest's summary line.
    """
    # Exactly one of the two sets applies to a deployment, so neither ever skips.
    unwanted = {"needs_contract_offer" if NO_CONTRACT_OFFER else "declares_no_contract_offer"}
    if HOLDER_UNSET:
        unwanted.add("needs_holder")
    if COLLECTOR_UNSET:
        unwanted.add("needs_collector")
    kept, dropped = [], []
    for item in items:
        marked = any(item.get_closest_marker(name) for name in unwanted)
        (dropped if marked else kept).append(item)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept
    config._holder_deselected = HOLDER_UNSET and any(
        item.get_closest_marker("needs_holder") for item in dropped
    )
    config._collector_deselected = COLLECTOR_UNSET and any(
        item.get_closest_marker("needs_collector") for item in dropped
    )


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Say that the holder checks did not run, in words, not only in a count."""
    if getattr(config, "_holder_deselected", False):
        terminalreporter.write_line(
            "DS CONTRACT: the holder checks were DESELECTED — "
            "DS_CONTRACT_HOLDER_CONNECTOR_URL and DS_CONTRACT_HOLDER_OFFER are unset, "
            "so reading an offer at another participant's connector was not checked.",
            yellow=True,
            bold=True,
        )
    if getattr(config, "_collector_deselected", False):
        terminalreporter.write_line(
            "DS CONTRACT: the collector checks were DESELECTED — "
            "DS_CONTRACT_COLLECTOR_CLIENT_ID and _SECRET are unset, so the consent "
            "checks ran as the pre-collector clients (the development transition), "
            "and svc-ds-collector-<alias>'s one-scope tokens were not checked.",
            yellow=True,
            bold=True,
        )


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


@pytest.fixture(scope="session")
def org_token() -> str:
    """An access token for the community's own organisation client.

    Not `allow_module_level`, unlike the service client's: this credential is
    needed by three checks and by nothing else, so a deployment that has not
    configured it should lose those three and keep the rest — where a missing
    service client means the whole half could not run.
    """
    skip_unconfigured(ORGANISATION_CREDENTIALS, "the organisation client that registers consent")

    try:
        resp = httpx.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": ORG_CLIENT_ID,
                "client_secret": ORG_CLIENT_SECRET,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]
    except Exception as exc:  # noqa: BLE001
        pytest.skip(_unreachable("the ds token endpoint", TOKEN_URL, exc))


@pytest.fixture(scope="session")
def org_auth(org_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {org_token}"}


def _client_token(client_id: str, client_secret: str, scope: str | None = None) -> str:
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    if scope is not None:
        data["scope"] = scope
    resp = httpx.post(TOKEN_URL, data=data, timeout=10)
    resp.raise_for_status()
    return resp.json()["access_token"]


@pytest.fixture(scope="session")
def collector_token():
    """``collector_token(scope)``: the collector client's token for that one scope.

    Only where the collector client is configured; checks that use it carry
    ``needs_collector`` and are deselected otherwise. Cached per scope, as the
    service caches it.
    """
    skip_unconfigured({**COLLECTOR, "DS_CONTRACT_TOKEN_URL": TOKEN_URL}, "the collector client")
    cache: dict[str, str] = {}

    def _token(scope: str) -> str:
        if scope not in cache:
            try:
                cache[scope] = _client_token(COLLECTOR_CLIENT_ID, COLLECTOR_CLIENT_SECRET, scope)
            except httpx.HTTPStatusError as exc:
                pytest.fail(
                    f"{COLLECTOR_CLIENT_ID} could not get a token for {scope!r}: "
                    f"{exc.response.status_code} {exc.response.text[:200]} — is the "
                    "scope an optional scope of the collector client?"
                )
        return cache[scope]

    return _token


@pytest.fixture(scope="session")
def acting_auth(request):
    """``acting_auth(scope)``: headers for an act this service performs for the community.

    The collector client with that one scope where it is configured; otherwise
    what the service does in its development transition — the organisation
    (connector) client for the consent write and read-backs, the service client
    for everything else.
    """

    def _auth(scope: str) -> dict[str, str]:
        if not COLLECTOR_UNSET:
            token = request.getfixturevalue("collector_token")(scope)
            return {"Authorization": f"Bearer {token}"}
        if scope in (CONSENT_PROVISION, CONSENT_COLLECTOR_READ):
            return dict(request.getfixturevalue("org_auth"))
        return dict(request.getfixturevalue("auth"))

    return _auth
