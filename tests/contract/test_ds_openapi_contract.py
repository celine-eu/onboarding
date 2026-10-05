"""Half one — every call we make is still in what ds publishes.

This is the half that would have caught the `DataDisclosed` 422: ds made
`dataset_id` required and this service kept sending a body without one, so every
disclosure was rejected and — because the emit was non-fatal — discarded in
silence.

It is **not** a general drift guard, and the plan says so. It catches a path that
moved, a method that went, a field that became required. It cannot catch a path
that stayed and changed meaning, which is what `/admin/owners/{owner_id}` did.
That is `test_ds_semantics.py`'s job.
"""

from __future__ import annotations

import dataclasses

import pytest
from celine.sdk.posture import is_dev

from contract.inventory import CALLS, Call

_VERBS = {"get", "post", "put", "patch", "delete"}
_ANNOUNCER = "ds-contract-fallback-announcer"


def accepted_method(spec: dict, call: Call, *, dev: bool) -> str | None:
    """The method ``call`` is checked under, or ``None`` when ds publishes neither.

    The primary ``method`` always counts. ``fallback_method`` counts **only in
    dev**: the local stack runs this service from source against whichever ds it
    has, and the code falls back there too. Outside dev the code refuses the
    fallback, so the check must too, or the drift it exists to show is hidden.
    """
    published = spec["paths"].get(call.path, {})
    if call.method in published:
        return call.method
    if dev and call.fallback_method and call.fallback_method in published:
        return call.fallback_method
    return None


class _FallbackAnnouncer:
    """Says, in the summary, which calls passed only on their dev fallback."""

    def __init__(self) -> None:
        self.lines: set[str] = set()

    def pytest_terminal_summary(self, terminalreporter):
        for line in sorted(self.lines):
            terminalreporter.write_line(line, yellow=True, bold=True)


def _announce_fallback(config: pytest.Config, call: Call) -> None:
    announcer = config.pluginmanager.get_plugin(_ANNOUNCER)
    if announcer is None:
        announcer = _FallbackAnnouncer()
        config.pluginmanager.register(announcer, _ANNOUNCER)
    announcer.lines.add(
        f"DS CONTRACT: ds {call.service} does not publish "
        f"{call.method.upper()} {call.path} yet — accepted its dev fallback "
        f"{(call.fallback_method or '').upper()} because CELINE_ENV=dev. Outside "
        "dev the primary method is required and this check fails."
    )


def _resolve(spec: dict, ref: str) -> dict:
    return spec["components"]["schemas"][ref.split("/")[-1]]


def required_fields(spec: dict, call: Call) -> set[str] | None:
    """What ds insists the request body carries, or None when it takes no body.

    **Resolves `oneOf`.** `/prov/events` accepts a union of a dozen event types
    and its top-level schema declares no `required` at all — so a checker that
    reads only the top level sees an empty set, passes, and misses exactly the
    failure that started this. `Call.variant` names the branch we mean.
    """
    op = spec["paths"][call.path][call.method]
    body = op.get("requestBody")
    if not body:
        return None

    schema = next(iter(body["content"].values()))["schema"]
    if "$ref" in schema:
        schema = _resolve(spec, schema["$ref"])

    if "oneOf" in schema or "anyOf" in schema:
        branches = schema.get("oneOf") or schema.get("anyOf")
        if not call.variant:
            raise AssertionError(
                f"{call.method.upper()} {call.path} takes a union body; "
                f"inventory.py must name which branch this call sends "
                f"(Call(..., variant=...))"
            )
        for branch in branches:
            resolved = _resolve(spec, branch["$ref"]) if "$ref" in branch else branch
            if resolved.get("title") == call.variant or branch.get("$ref", "").endswith(
                f"/{call.variant}"
            ):
                schema = resolved
                break
        else:
            raise AssertionError(
                f"{call.method.upper()} {call.path}: no branch named "
                f"{call.variant!r} — ds may have renamed or removed it"
            )

    return set(schema.get("required") or [])


@pytest.mark.ds_contract
@pytest.mark.parametrize("call", CALLS, ids=lambda c: f"{c.method.upper()} {c.path}")
def test_the_call_still_exists(specs, call: Call, request):
    spec = specs[call.service]
    assert call.path in spec["paths"], (
        f"ds {call.service} no longer publishes {call.path}. {call.why}"
    )
    published = sorted(m for m in spec["paths"][call.path] if m in _VERBS)
    method = accepted_method(spec, call, dev=is_dev())
    assert method, (
        f"ds {call.service} no longer accepts {call.method.upper()} on "
        f"{call.path} (has: {published}). {call.why}"
    )
    if method != call.method:
        _announce_fallback(request.config, call)


@pytest.mark.ds_contract
@pytest.mark.parametrize("call", CALLS, ids=lambda c: f"{c.method.upper()} {c.path}")
def test_we_send_every_field_ds_requires(specs, call: Call):
    spec = specs[call.service]
    method = accepted_method(spec, call, dev=is_dev())
    if method is None:
        pytest.skip("covered by test_the_call_still_exists")
    # On the dev fallback, check the fallback's own request (a GET takes no body).
    call = dataclasses.replace(call, method=method)

    required = required_fields(spec, call)
    if required is None:
        return

    missing = required - set(call.sends)
    assert not missing, (
        f"ds {call.service} now requires {sorted(missing)} on "
        f"{call.method.upper()} {call.path}, and this service does not send it. "
        f"Every request will be answered 422. {call.why}"
    )


@pytest.mark.ds_contract
def test_the_checker_can_see_the_failure_that_started_this(specs):
    """A self-test of the union resolution above, against the real spec.

    This service no longer posts to `/prov/events`, so nothing in the inventory
    exercises the `oneOf` path — and an unexercised resolver is one that quietly
    stops working. `DataDisclosed` is the branch whose newly-required
    `dataset_id` broke us, so asserting the checker can still see it keeps the
    mechanism honest without asserting a call we do not make.
    """
    spec = specs["provenance"]
    probe = Call("provenance", "post", "/prov/events", variant="DataDisclosed")

    top_level = next(
        iter(spec["paths"]["/prov/events"]["post"]["requestBody"]["content"].values())
    )["schema"]
    assert "oneOf" in top_level, (
        "the union shape is gone — re-read the spec before trusting this check"
    )

    required = required_fields(spec, probe)
    assert "dataset_id" in required and "consent_snapshot_hash" in required, (
        "DataDisclosed no longer requires the fields whose absence made every "
        f"emit a silent 422 (requires: {sorted(required)})"
    )
