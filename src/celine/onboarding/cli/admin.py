"""Review and enablement from the terminal.

The same flow the console drives, over the same API, so the two cannot answer
differently. Every read takes `--json` so this is scriptable; every write prints
what changed.

Submissions are addressed by their **reference** — the string printed on the
participant's confirmation and quoted in every email — rather than by UUID.
Resolution goes through the queue's `ref` filter, and an ambiguous prefix is
refused rather than guessed at.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import typer

from celine.onboarding.cli.transport import CliError, Transport, build

app = typer.Typer(name="admin", help="Review submissions and repair enablement")
review_app = typer.Typer(help="Drive the review state machine")
enablement_app = typer.Typer(help="Inspect and repair what approval did")
app.add_typer(review_app, name="review")
app.add_typer(enablement_app, name="enablement")

_LOCAL = typer.Option(
    False,
    "--local",
    help="Talk to the database directly, bypassing the API and its authorization. "
    "Break-glass for a deployment with no Keycloak; needs ALLOW_LOCAL_ADMIN=true.",
)
_API_URL = typer.Option(None, "--api-url", help="Override ONBOARDING_API_URL")
_TOKEN = typer.Option(
    None, "--token", help="Use this bearer token instead of a client-credentials grant"
)
_JSON = typer.Option(False, "--json", help="Emit JSON instead of a table")


def _run(coro):
    """Run one command, turning CliError into a clean message and exit code."""

    async def _wrapped():
        try:
            return await coro
        except CliError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None

    return asyncio.run(_wrapped())


async def _resolve(transport: Transport, rec: str, ref: str) -> dict:
    """Find one submission by reference, id, or unambiguous partial reference."""
    rows, _ = await transport.list_submissions(rec, status=None, ref=ref, limit=50)
    exact = [r for r in rows if r["ref"] == ref or r["id"] == ref]
    if exact:
        return exact[0]
    if not rows:
        raise CliError(f"No submission in {rec!r} matches {ref!r}")
    if len(rows) > 1:
        listed = ", ".join(sorted(r["ref"] for r in rows[:5]))
        raise CliError(
            f"{ref!r} matches {len(rows)} submissions in {rec!r} ({listed}…). "
            "Use the full reference."
        )
    return rows[0]


def _emit(payload: Any, as_json: bool, renderer) -> None:
    if as_json:
        typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        renderer(payload)


def _print_submissions(rows: list[dict]) -> None:
    if not rows:
        typer.echo("No submissions.")
        return
    typer.echo(f"{'REFERENCE':<22} {'STATUS':<13} {'NAME':<26} EMAIL")
    for row in rows:
        name = " ".join(filter(None, [row.get("first_name"), row.get("last_name")])) or "-"
        typer.echo(
            f"{row['ref']:<22} {row['status']:<13} {name[:25]:<26} {row.get('email') or '-'}"
        )


def _print_enablement(payload: dict) -> None:
    typer.echo(f"state: {payload['state']}")
    typer.echo(f"{'STEP':<22} {'STATUS':<11} {'TRIES':<6} DETAIL")
    for step in payload["steps"]:
        note = step.get("last_error") or step.get("detail") or ""
        blocking = "" if step["fail_closed"] else "  (non-blocking)"
        colour = {
            "failed": typer.colors.RED,
            "succeeded": typer.colors.GREEN,
            "skipped": typer.colors.BRIGHT_BLACK,
        }.get(step["status"])
        line = (
            f"{step['step']:<22} {step['status']:<11} {step['attempts']:<6} {note[:60]}{blocking}"
        )
        typer.secho(line, fg=colour)


def _print_submission(row: dict) -> None:
    for key in (
        "ref",
        "id",
        "rec_slug",
        "status",
        "first_name",
        "last_name",
        "email",
        "phone",
        "phone_verified",
        "fiscal_code",
        "pod_code",
        "supply_municipality",
        "gdpr_consent",
        "policy_consent",
        "statute_consent",
        "data_sharing_consent",
        "share_provisioned",
        "dataspace_did",
        "notes",
        "created_at",
        "updated_at",
    ):
        if key in row:
            typer.echo(f"{key:<24} {row[key]}")


# ---------------------------------------------------------------------------
# whoami
# ---------------------------------------------------------------------------


@app.command("whoami")
def whoami(
    local: bool = _LOCAL, api_url: str = _API_URL, token: str = _TOKEN, as_json: bool = _JSON
):
    """Who the CLI authenticates as, and what it may do where."""

    async def _go():
        transport = build(local, api_url=api_url, token=token)
        try:
            me = await transport.whoami()
        finally:
            await transport.aclose()

        def _render(payload: dict) -> None:
            typer.echo(f"subject   {payload['sub']} ({payload['subject_type']})")
            if payload.get("email"):
                typer.echo(f"email     {payload['email']}")
            if payload.get("realm_groups"):
                typer.echo(f"realm     {', '.join(payload['realm_groups'])}")
            for rec in payload["recs"]:
                typer.echo(
                    f"  {rec['slug']:<20} org={rec['organization'] or '-':<20} "
                    f"{len(rec['capabilities'])} capabilities"
                )

        _emit(me, as_json, _render)

    _run(_go())


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------


@review_app.command("list")
def review_list(
    rec: str = typer.Option(..., "--rec", "-r", help="Community slug"),
    status: str = typer.Option(
        None, "--status", "-s", help="draft|submitted|under_review|approved|rejected"
    ),
    ref: str = typer.Option(None, "--ref", help="Substring of the submission reference"),
    limit: int = typer.Option(50, "--limit", "-n"),
    local: bool = _LOCAL,
    api_url: str = _API_URL,
    token: str = _TOKEN,
    as_json: bool = _JSON,
):
    """The review queue for a community."""

    async def _go():
        transport = build(local, api_url=api_url, token=token)
        try:
            rows, total = await transport.list_submissions(rec, status=status, ref=ref, limit=limit)
        finally:
            await transport.aclose()

        if as_json:
            typer.echo(json.dumps({"total": total, "submissions": rows}, indent=2))
        else:
            _print_submissions(rows)
            if total > len(rows):
                typer.echo(f"\nShowing {len(rows)} of {total}. Raise --limit to see more.")

    _run(_go())


@review_app.command("show")
def review_show(
    ref: str = typer.Argument(..., help="Submission reference"),
    rec: str = typer.Option(..., "--rec", "-r"),
    reveal: bool = typer.Option(False, "--reveal", help="Unmask the fiscal code and POD. Audited."),
    local: bool = _LOCAL,
    api_url: str = _API_URL,
    token: str = _TOKEN,
    as_json: bool = _JSON,
):
    """One submission in full."""

    async def _go():
        transport = build(local, api_url=api_url, token=token)
        try:
            found = await _resolve(transport, rec, ref)
            row = await transport.get_submission(rec, found["id"], reveal=reveal)
        finally:
            await transport.aclose()
        _emit(row, as_json, _print_submission)

    _run(_go())


def _transition_command(name: str, target: str, help_text: str, *, needs_reason: bool = False):
    @review_app.command(name, help=help_text)
    def _command(
        ref: str = typer.Argument(..., help="Submission reference"),
        rec: str = typer.Option(..., "--rec", "-r"),
        reason: str = typer.Option(None, "--reason", help="Why. Required when rejecting."),
        local: bool = _LOCAL,
        api_url: str = _API_URL,
        token: str = _TOKEN,
        as_json: bool = _JSON,
    ):
        if needs_reason and not (reason or "").strip():
            typer.secho(
                "--reason is required when rejecting: the participant is told, and "
                "whoever reopens the case months later needs to know why.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(1)

        async def _go():
            transport = build(local, api_url=api_url, token=token)
            try:
                found = await _resolve(transport, rec, ref)
                try:
                    row = await transport.transition(rec, found["id"], target, reason)
                except CliError:
                    # An approval blocked by enablement is the common case, and the
                    # step table is the answer to "what now" — so print it rather
                    # than leaving the operator to run a second command.
                    #
                    # Only when the pipeline actually ran, though: approval can
                    # also be refused before it starts (an unverified phone, a
                    # transition that is not allowed), and printing a table of
                    # untouched steps would suggest it was tried and failed.
                    if target == "approved":
                        state = await transport.enablement(rec, found["id"])
                        if state["state"] != "not_started":
                            typer.secho(
                                "Approval blocked. Enablement state:",
                                fg=typer.colors.YELLOW,
                                err=True,
                            )
                            _print_enablement(state)
                    raise
                if target == "approved" and not as_json:
                    _print_enablement(await transport.enablement(rec, found["id"]))
            finally:
                await transport.aclose()

            if as_json:
                typer.echo(json.dumps(row, indent=2))
            else:
                typer.secho(
                    f"{found['ref']}: {found['status']} -> {row['status']}",
                    fg=typer.colors.GREEN,
                )

        _run(_go())

    return _command


_transition_command("take", "under_review", "Take a submission in charge.")
_transition_command("approve", "approved", "Approve, enabling the participant.")
_transition_command("reject", "rejected", "Reject, with a reason.", needs_reason=True)
_transition_command("reopen", "submitted", "Put a rejected submission back in the queue.")


# ---------------------------------------------------------------------------
# enablement
# ---------------------------------------------------------------------------


@enablement_app.command("status")
def enablement_status(
    ref: str = typer.Argument(..., help="Submission reference"),
    rec: str = typer.Option(..., "--rec", "-r"),
    local: bool = _LOCAL,
    api_url: str = _API_URL,
    token: str = _TOKEN,
    as_json: bool = _JSON,
):
    """What approval did, step by step."""

    async def _go():
        transport = build(local, api_url=api_url, token=token)
        try:
            found = await _resolve(transport, rec, ref)
            payload = await transport.enablement(rec, found["id"])
        finally:
            await transport.aclose()
        _emit(payload, as_json, _print_enablement)

    _run(_go())


@enablement_app.command("retry")
def enablement_retry(
    ref: str = typer.Argument(..., help="Submission reference"),
    rec: str = typer.Option(..., "--rec", "-r"),
    step: str = typer.Option(
        None,
        "--step",
        help="keycloak_user | rec_registry_member | dataspace_identity | "
        "dataspace_share. Omit to re-run everything unfinished.",
    ),
    local: bool = _LOCAL,
    api_url: str = _API_URL,
    token: str = _TOKEN,
    as_json: bool = _JSON,
):
    """Re-run the steps that have not succeeded."""

    async def _go():
        transport = build(local, api_url=api_url, token=token)
        try:
            found = await _resolve(transport, rec, ref)
            payload = await transport.retry(rec, found["id"], step)
        finally:
            await transport.aclose()
        _emit(payload, as_json, _print_enablement)
        if payload["state"] == "failed":
            raise typer.Exit(1)

    _run(_go())


@enablement_app.command("revoke")
def enablement_revoke(
    ref: str = typer.Argument(..., help="Submission reference"),
    rec: str = typer.Option(..., "--rec", "-r"),
    confirm: bool = typer.Option(
        False, "--confirm", help="Required; this is not reversible in one step."
    ),
    local: bool = _LOCAL,
    api_url: str = _API_URL,
    token: str = _TOKEN,
    as_json: bool = _JSON,
):
    """Undo enablement: credential, membership, registry member, login.

    Does not withdraw the standing sharing consent — that is the data subject's
    own act, made with their own credential.
    """
    if not confirm:
        typer.secho(
            "Pass --confirm. This revokes a credential and deactivates a member.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)

    async def _go():
        transport = build(local, api_url=api_url, token=token)
        try:
            found = await _resolve(transport, rec, ref)
            payload = await transport.revoke(rec, found["id"])
        finally:
            await transport.aclose()
        _emit(payload, as_json, _print_enablement)

    _run(_go())


# ---------------------------------------------------------------------------
# purge and audit
# ---------------------------------------------------------------------------


@app.command("purge")
def purge(
    ref: str = typer.Argument(..., help="Submission reference"),
    rec: str = typer.Option(..., "--rec", "-r"),
    confirm: bool = typer.Option(False, "--confirm", help="Required. Erasure is permanent."),
    local: bool = _LOCAL,
    api_url: str = _API_URL,
    token: str = _TOKEN,
):
    """GDPR erasure: files from disk, rows from the database."""
    if not confirm:
        typer.secho("Pass --confirm. This is permanent.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    async def _go():
        transport = build(local, api_url=api_url, token=token)
        try:
            found = await _resolve(transport, rec, ref)
            await transport.purge(rec, found["id"])
        finally:
            await transport.aclose()
        typer.secho(f"Erased {found['ref']}.", fg=typer.colors.GREEN)

    _run(_go())


@app.command("audit")
def audit(
    rec: str = typer.Option(..., "--rec", "-r"),
    action: str = typer.Option(None, "--action", help="e.g. transition, reveal, delete"),
    actor: str = typer.Option(None, "--actor", help="Substring of the actor's subject or email"),
    limit: int = typer.Option(50, "--limit", "-n"),
    local: bool = _LOCAL,
    api_url: str = _API_URL,
    token: str = _TOKEN,
    as_json: bool = _JSON,
):
    """This community's audit trail."""

    async def _go():
        transport = build(local, api_url=api_url, token=token)
        try:
            rows = await transport.audit(rec, limit=limit, action=action, actor=actor)
        finally:
            await transport.aclose()

        def _render(entries: list[dict]) -> None:
            if not entries:
                typer.echo("No audit entries.")
                return
            typer.echo(f"{'WHEN':<28} {'ACTION':<20} {'ACTOR':<28} DETAIL")
            for entry in entries:
                who = entry.get("actor_email") or entry.get("actor_sub") or entry["actor_type"]
                typer.echo(
                    f"{entry['created_at']:<28} {entry['action']:<20} {who[:27]:<28} "
                    f"{entry.get('detail') or ''}"
                )

        _emit(rows, as_json, _render)

    _run(_go())
