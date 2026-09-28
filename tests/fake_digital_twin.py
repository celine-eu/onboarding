"""A Digital Twin in front of the real SDK client, for the boundary tests.

`respx` answers the two value fetchers over HTTP, so the request that leaves this
service — path, token, payload — is the one production sends, through
celine-sdk's `DTClient`. The fake decides containment on **synthetic squares**
the way the Digital Twin does (D28): a point on an edge is inside, and a point in
two shapes resolves to the lowest id. No real boundary, code or place is used.

    AC000E00001  lon 0.0–0.1, lat 0.0–0.1
    AC000E00002  lon 0.1–0.2, lat 0.0–0.1   (shares the edge lon = 0.1)
    AC000E00003  lon 0.3–0.4, lat 0.0–0.1

The squares lie in the open sea south of the Gulf of Guinea, off every coast, so
no test point is anybody's address.
"""

from __future__ import annotations

import json

import httpx
import respx

DT_URL = "http://dt.test"
SERVICE_TOKEN = "svc-onboarding-token"

SQUARES: dict[str, tuple[float, float, float, float]] = {
    # id: (lon_min, lat_min, lon_max, lat_max)
    "AC000E00001": (0.0, 0.0, 0.1, 0.1),
    "AC000E00002": (0.1, 0.0, 0.2, 0.1),
    "AC000E00003": (0.3, 0.0, 0.4, 0.1),
}

#: Points, as (lat, lon), and where the fake puts them.
INSIDE_1 = (0.0473, 0.0519)  # AC000E00001
INSIDE_2 = (0.0473, 0.1537)  # AC000E00002
INSIDE_3 = (0.0473, 0.3581)  # AC000E00003
ON_SHARED_EDGE = (0.0473, 0.1)  # both 1 and 2: the lowest id, AC000E00001
OUTSIDE_ALL = (0.0473, 0.2566)  # none

SOURCES = {"gse_cabine_primarie"}


class FakeDigitalTwin:
    def __init__(self, router: respx.MockRouter) -> None:
        #: The respx router, so a test can put other services behind it too.
        self.router = router
        self.requests: list[tuple[str, str, dict, str]] = []
        #: Set to an exception instance to make every call raise it (e.g. a
        #: timeout), or to an int to answer every call with that status.
        self.failure: Exception | int | None = None
        #: Set to a callable (fetcher, payload) -> httpx.Response to answer
        #: something the fake would not.
        self.override = None
        router.post(
            url__regex=rf"^{DT_URL}/communities/it/(?P<community>[^/]+)/values/(?P<fetcher>[^/]+)$"
        ).mock(side_effect=self._answer)

    def calls(self, fetcher: str | None = None) -> list[tuple[str, str, dict, str]]:
        return [r for r in self.requests if fetcher is None or r[1] == fetcher]

    def _answer(self, request: httpx.Request, community: str, fetcher: str) -> httpx.Response:
        payload = json.loads(request.content)["payload"]
        auth = request.headers.get("authorization", "")
        self.requests.append((community, fetcher, payload, auth))

        if self.override is not None:
            return self.override(fetcher, payload)
        if isinstance(self.failure, Exception):
            raise self.failure
        if isinstance(self.failure, int):
            return httpx.Response(
                self.failure, json={"detail": {"error": "failure", "echo": payload}}
            )

        if payload.get("source") not in SOURCES:
            # As the Digital Twin does: a closed enum, 400, and the refusal body
            # echoes the payload — which is why nothing may log it.
            return httpx.Response(
                400, json={"detail": {"error": "validation_error", "payload": payload}}
            )

        if fetcher == "boundary_at_point":
            lat, lon = payload["lat"], payload["lon"]
            covering = sorted(
                i for i, (x0, y0, x1, y1) in SQUARES.items() if x0 <= lon <= x1 and y0 <= lat <= y1
            )
            items = [{"id": covering[0]}] if covering else []
            return _result(items, limit=1)

        if fetcher == "boundary_shape":
            ids = payload["ids"]
            if len(ids) > 100:
                return httpx.Response(400, json={"detail": {"error": "validation_error"}})
            items = [
                {"id": i, "geojson": json.dumps(_polygon(SQUARES[i]))}
                for i in sorted(set(ids))
                if i in SQUARES
            ]
            return _result(items, limit=100)

        return httpx.Response(404, json={"detail": "unknown fetcher"})


def _result(items: list[dict], *, limit: int) -> httpx.Response:
    return httpx.Response(
        200, json={"items": items, "count": len(items), "limit": limit, "offset": 0}
    )


def _polygon(square: tuple[float, float, float, float]) -> dict:
    x0, y0, x1, y1 = square
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


def running_fake_dt(monkeypatch):
    """The fake Digital Twin, reached through the real SDK client with a static token.

    A generator for the `fake_dt` fixture in `conftest.py`."""
    from celine.sdk.auth import StaticTokenProvider

    from celine.onboarding.config.settings import settings
    from celine.onboarding.services import boundaries

    monkeypatch.setattr(settings, "digital_twin_url", DT_URL)
    monkeypatch.setattr(boundaries, "_token_provider", lambda: StaticTokenProvider(SERVICE_TOKEN))
    boundaries.reset_client()
    with respx.mock(assert_all_called=False) as router:
        yield FakeDigitalTwin(router)
    boundaries.reset_client()


def boundary_manifest(slug: str = "rec-b", **areas: str) -> dict:
    """A manifest whose areas are boundaries: ``north="AC000E00001"``."""
    return {
        "slug": slug,
        "name": slug,
        "steps": ["consents", "personal", "eligibility", "review"],
        "rec_registry": {
            "community": "example-rec",
            "areas": {
                key: {"boundary": {"source": "gse_cabine_primarie", "id": cod}}
                for key, cod in areas.items()
            },
        },
    }
