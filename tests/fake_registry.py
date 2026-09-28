"""A REC registry and a provisioning service behind `respx`, for the registry sync.

The registry answers the routes a sync uses as rec-registry 1.6.0 does, with its
rules and its codes (`{detail, code}` on a refusal):

- `GET /admin/communities/{c}` — the community, its `areas` and `topology`;
  `404` for an unknown one.
- `GET /admin/communities/{c}/members?area=&limit=&cursor=` — paged.
- `PUT|DELETE /admin/communities/{c}/topology/{id}` — merge by id; the body `id`
  must match the path (`422`); a node an area lists is `409
  topology_node_in_use`; an absent one `404` with no code.
- `PUT|DELETE /admin/communities/{c}/areas/{k}` — one boundary, one
  `primary_substation` node with the same id, no two areas on one boundary
  (`422 invalid_area_boundary`); an area members reference is `409 area_in_use`.
- `POST /admin/communities/{c}/areas/{k}/rename` `{new_key}` — the area as
  stored moves to `new_key` with every member naming it, in one step, answering
  `{old_key, new_key, members_moved, community}`; `404 area_not_found`, `409
  area_key_taken`, `422 invalid_area_key`, `422` for any other body.

Every request is recorded with its token, so a test can say which scope a call
carried. Synthetic data only: `AC000E0000x` ids, `example-rec`, members named
`Example Person n`.
"""

from __future__ import annotations

import copy
import json
import re

import httpx
import respx

REGISTRY_URL = "http://registry.test"
PROVISIONING_URL = "http://provisioning.test"
COMMUNITY = "example-rec"


def _refuse(status: int, detail: str, code: str | None = None) -> httpx.Response:
    body: dict = {"detail": detail}
    if code:
        body["code"] = code
    return httpx.Response(status, json=body)


class FakeRegistry:
    def __init__(self, router: respx.MockRouter) -> None:
        self.communities: dict[str, dict] = {
            COMMUNITY: {"key": COMMUNITY, "name": "Example REC", "areas": {}, "topology": []}
        }
        #: community -> [{key, name, area}]
        self.members: dict[str, list[dict]] = {COMMUNITY: []}
        #: (method, path, bearer token) of every call.
        self.requests: list[tuple[str, str, str]] = []
        #: Set to an exception to make every call raise it.
        self.failure: Exception | None = None
        base = rf"^{REGISTRY_URL}/admin/communities/(?P<c>[^/?]+)"
        # Dispatched through the attribute at call time, so a test can replace
        # one route's behaviour by assigning to it.
        router.get(url__regex=base + r"$").mock(
            side_effect=lambda request, **kw: self._community(request, **kw)
        )
        router.get(url__regex=base + r"/members(\?.*)?$").mock(
            side_effect=lambda request, **kw: self._list_members(request, **kw)
        )
        router.route(url__regex=base + r"/topology/(?P<node>[^/?]+)$").mock(
            side_effect=lambda request, **kw: self._node(request, **kw)
        )
        router.route(url__regex=base + r"/areas/(?P<area>[^/?]+)$").mock(
            side_effect=lambda request, **kw: self._area(request, **kw)
        )
        router.post(url__regex=base + r"/areas/(?P<area>[^/?]+)/rename$").mock(
            side_effect=lambda request, **kw: self._rename(request, **kw)
        )

    # -- helpers for tests -------------------------------------------------

    def add_member(self, area: str, n: int = 1, community: str = COMMUNITY) -> None:
        start = len(self.members[community])
        for i in range(n):
            self.members[community].append(
                {"key": f"m-{start + i:04d}", "name": f"Example Person {start + i}", "area": area}
            )

    def seed_area(self, key: str, boundary_id: str, *, name: str | None = None) -> None:
        community = self.communities[COMMUNITY]
        community["topology"] = [n for n in community["topology"] if n["id"] != boundary_id] + [
            {"id": boundary_id, "type": "primary_substation", "name": name or key}
        ]
        community["areas"][key] = {
            "name": name or key,
            "boundary": {"source": "gse_cabine_primarie", "id": boundary_id},
            "topology": [boundary_id],
        }

    def writes(self) -> list[tuple[str, str, str]]:
        return [r for r in self.requests if r[0] in ("PUT", "DELETE", "POST")]

    def reads(self) -> list[tuple[str, str, str]]:
        return [r for r in self.requests if r[0] == "GET"]

    def snapshot(self) -> dict:
        return copy.deepcopy(self.communities[COMMUNITY])

    # -- routes ------------------------------------------------------------

    def _record(self, request: httpx.Request) -> None:
        if self.failure is not None:
            raise self.failure
        auth = request.headers.get("authorization", "")
        self.requests.append((request.method, request.url.path, auth.removeprefix("Bearer ")))

    def _get(self, c: str) -> dict | None:
        return self.communities.get(c)

    def _detail(self, c: str) -> httpx.Response:
        community = self.communities[c]
        topology = [
            {
                "id": n["id"],
                "type": n["type"],
                "name": n.get("name"),
                "operator_id": n.get("operator_id"),
                "parent": n.get("parent"),
                "area": n.get("area") or {},
            }
            for n in community["topology"]
        ]
        areas = {
            k: {
                "name": a["name"],
                "boundary": a.get("boundary"),
                "topology": a.get("topology") or [],
                "location": a.get("location"),
                "geometry": a.get("geometry"),
            }
            for k, a in community["areas"].items()
        }
        return httpx.Response(
            200,
            json={
                "id": "c-uuid",
                "key": c,
                "name": community["name"],
                "areas": areas,
                "topology": topology,
            },
        )

    def _community(self, request: httpx.Request, c: str) -> httpx.Response:
        self._record(request)
        if self._get(c) is None:
            return _refuse(404, "Community not found")
        return self._detail(c)

    def _list_members(self, request: httpx.Request, c: str) -> httpx.Response:
        self._record(request)
        if self._get(c) is None:
            return _refuse(404, "Community not found")
        area = request.url.params.get("area")
        limit = int(request.url.params.get("limit", 50))
        if limit > 500:
            return _refuse(422, "limit too large")
        cursor = request.url.params.get("cursor")
        rows = sorted(
            (m for m in self.members[c] if area is None or m["area"] == area),
            key=lambda m: m["key"],
        )
        if cursor:
            rows = [m for m in rows if m["key"] > cursor]
        page = rows[:limit]
        next_cursor = page[-1]["key"] if len(page) == limit else None
        return httpx.Response(200, json={"items": page, "next_cursor": next_cursor})

    def _node(self, request: httpx.Request, c: str, node: str) -> httpx.Response:
        self._record(request)
        community = self._get(c)
        if community is None:
            return _refuse(404, f"Community {c!r} not found", "community_not_found")
        topology = community["topology"]
        if request.method == "PUT":
            body = json.loads(request.content)
            if body.get("id") != node:
                return _refuse(422, "Body id does not match path id")
            kept = {k: body[k] for k in ("id", "type", "name", "operator_id", "parent", "area")
                    if k in body}  # fmt: skip
            for area_key, area in community["areas"].items():
                if (
                    node in (area.get("topology") or [])
                    and kept.get("type") != "primary_substation"
                ):
                    return _refuse(
                        422, f"area {area_key!r}: its node would not be a primary substation",
                        "invalid_area_boundary",
                    )  # fmt: skip
            if any(n["id"] == node for n in topology):
                community["topology"] = [kept if n["id"] == node else n for n in topology]
            else:
                topology.append(kept)
            return self._detail(c)
        if not any(n["id"] == node for n in topology):
            return _refuse(404, f"Topology node {node!r} not found")
        users = sorted(
            k for k, a in community["areas"].items() if node in (a.get("topology") or [])
        )
        if users:
            return _refuse(
                409, f"Topology node {node!r} is still referenced by area(s) {users}",
                "topology_node_in_use",
            )  # fmt: skip
        community["topology"] = [n for n in topology if n["id"] != node]
        return self._detail(c)

    def _area(self, request: httpx.Request, c: str, area: str) -> httpx.Response:
        self._record(request)
        community = self._get(c)
        if community is None:
            return _refuse(404, f"Community {c!r} not found", "community_not_found")
        areas = community["areas"]
        if request.method == "PUT":
            body = json.loads(request.content)
            boundary = body.get("boundary") or {}
            bid = boundary.get("id")
            nodes = body.get("topology") or []
            node = next((n for n in community["topology"] if n["id"] == bid), None)
            if (
                not bid
                or nodes != [bid]
                or node is None
                or node.get("type") != "primary_substation"
            ):
                return _refuse(422, f"area {area!r} breaks the area rule", "invalid_area_boundary")
            others = [
                k
                for k, a in areas.items()
                if k != area and (a.get("boundary") or {}).get("id") == bid
            ]
            if others:
                return _refuse(
                    422, f"areas {sorted([area, *others])} reference one boundary",
                    "invalid_area_boundary",
                )  # fmt: skip
            areas[area] = body
            return self._detail(c)
        if area not in areas:
            return _refuse(404, f"Area {area!r} not found")
        in_use = sum(1 for m in self.members[c] if m["area"] == area)
        if in_use:
            return _refuse(
                409, f"Area {area!r} is still referenced by {in_use} member(s); move them first",
                "area_in_use",
            )  # fmt: skip
        del areas[area]
        return self._detail(c)

    def _rename(self, request: httpx.Request, c: str, area: str) -> httpx.Response:
        self._record(request)
        community = self._get(c)
        if community is None:
            return _refuse(404, f"Community {c!r} not found", "community_not_found")
        body = json.loads(request.content)
        if set(body) != {"new_key"} or not isinstance(body["new_key"], str):
            return httpx.Response(422, json={"detail": [{"msg": "invalid body"}]})
        new_key = body["new_key"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", new_key):
            return _refuse(422, f"{new_key!r} is not an area key", "invalid_area_key")
        areas = community["areas"]
        if area not in areas:
            return _refuse(404, f"Area {area!r} not found", "area_not_found")
        if new_key in areas:
            return _refuse(409, f"Area {new_key!r} already exists", "area_key_taken")
        community["areas"] = {(new_key if k == area else k): v for k, v in areas.items()}
        moved = 0
        for member in self.members[c]:
            if member["area"] == area:
                member["area"] = new_key
                moved += 1
        detail = json.loads(self._detail(c).content)
        return httpx.Response(
            200,
            json={"old_key": area, "new_key": new_key, "members_moved": moved, "community": detail},
        )


class FakeProvisioning:
    """`POST /reconcile/{community}`, answering what a test sets."""

    def __init__(self, router: respx.MockRouter) -> None:
        #: (community, bearer token) of every call.
        self.calls: list[tuple[str, str]] = []
        #: None: answer 200. An int: that status with the service's error body.
        #: An exception: raise it.
        self.failure: int | Exception | None = None
        self.code = "provisioning_failed"
        router.post(url__regex=rf"^{PROVISIONING_URL}/reconcile/(?P<c>[^/]+)$").mock(
            side_effect=lambda request, **kw: self._reconcile(request, **kw)
        )

    def _reconcile(self, request: httpx.Request, c: str) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        self.calls.append((c, auth.removeprefix("Bearer ")))
        if isinstance(self.failure, Exception):
            raise self.failure
        if isinstance(self.failure, int):
            return httpx.Response(
                self.failure, json={"detail": {"code": self.code, "message": "refused"}}
            )
        return httpx.Response(
            200,
            json={"community": c, "members": 0, "created": 0, "existing": 0, "divergences": []},
        )
