import asyncio
from datetime import UTC, datetime
from pathlib import Path

import typer

from celine.onboarding.cli.admin import _API_URL, _LOCAL, _TOKEN, _run
from celine.onboarding.cli.admin import app as admin_app
from celine.onboarding.cli.transport import build
from celine.onboarding.config.settings import settings

app = typer.Typer(name="onboarding-cli", help="REC Onboarding CLI")

# Review and enablement. Registered as a sub-app rather than flat commands so
# `onboarding-cli --help` separates the deployment tasks (importing templates,
# exporting) from the per-submission ones.
app.add_typer(admin_app, name="admin")


async def _load_recs() -> None:
    """Fill the REC manifest cache, as the API's startup hook does.

    For the commands that still read the database directly (`check-offers`).
    ``template_service.load_manifest`` reads a module-level cache that only the
    API's startup and the ``admin --local`` transport fill; a command that skipped
    this found every community missing — ``KeyError: REC '<slug>' not found`` for
    a REC that was imported and active.
    """
    from celine.onboarding.services import template_service

    await template_service.load_recs_from_db()


@app.command()
def import_templates(
    filter: str | None = typer.Option(None, "--filter", "-f", help="Import only this slug"),
    all: bool = typer.Option(False, "--all", "-a", help="Import all discovered templates"),
    templates_dir: str | None = typer.Option(
        None, "--templates-dir", help="Override templates directory"
    ),
):
    """Import template manifests from disk into the database."""
    import yaml

    from celine.onboarding.models.database import async_session

    tpl_root = Path(templates_dir) if templates_dir else Path(settings.templates_dir)
    if not tpl_root.is_dir():
        typer.echo(f"Templates directory not found: {tpl_root}", err=True)
        raise typer.Exit(1)

    if not filter and not all:
        typer.echo("Specify --filter SLUG or --all", err=True)
        raise typer.Exit(1)

    manifests: list[tuple[str, str, dict]] = []
    for subdir in sorted(tpl_root.iterdir()):
        if not subdir.is_dir():
            continue
        manifest_path = subdir / "manifest.yaml"
        if not manifest_path.exists():
            continue
        with open(manifest_path, encoding="utf-8") as f:
            manifest = yaml.safe_load(f)
        slug = manifest.get("slug", subdir.name)
        if slug != subdir.name:
            typer.echo(
                f"  Warning: slug '{slug}' does not match directory "
                f"'{subdir.name}', using directory name"
            )
            slug = subdir.name
            manifest["slug"] = slug
        name = manifest.get("name", slug)
        if filter and slug != filter:
            continue

        # Validate the dataspace binding here rather than at approval time. The
        # alias must match an owner id in the deployment's owners.yaml exactly,
        # and a typo should fail where an operator is already looking — not the
        # first time a REC manager approves somebody.
        from celine.onboarding.services.template_service import (
            validate_boundary_template,
            validate_data_sharing_recipients,
            validate_data_sharing_summary,
            validate_data_sharing_texts,
            validate_dataspace_block,
            validate_existing_members,
            validate_organization,
            validate_rec_registry_block,
        )

        try:
            validate_organization(manifest, where=str(manifest_path))
            validate_dataspace_block(manifest.get("dataspace"), where=str(manifest_path))
            validate_rec_registry_block(manifest.get("rec_registry"), where=str(manifest_path))
            validate_boundary_template(manifest, where=str(manifest_path))
            validate_existing_members(manifest.get("existing_members"), where=str(manifest_path))
            validate_data_sharing_texts(
                ((manifest.get("consent") or {}).get("data_sharing") or {}).get("texts"),
                where=str(manifest_path),
            )
            validate_data_sharing_recipients(
                ((manifest.get("consent") or {}).get("data_sharing") or {}).get("recipients"),
                where=str(manifest_path),
            )
            validate_data_sharing_summary(
                ((manifest.get("consent") or {}).get("data_sharing") or {}).get("summary"),
                where=str(manifest_path),
            )
        except ValueError as exc:
            typer.echo(f"  {exc}", err=True)
            raise typer.Exit(1) from exc

        manifests.append((slug, name, manifest))

    if not manifests:
        typer.echo("No templates found to import.")
        raise typer.Exit(1)

    # Every boundary a template names must be one the Digital Twin knows
    # (REQ-0002). Checked for all of them before any is stored, and a Digital
    # Twin that cannot be asked refuses the import rather than letting an
    # unvalidated template through. A template without boundaries asks nothing.
    from celine.onboarding.services.boundaries import verify_template_boundaries

    async def _verify():
        for slug, _name, manifest in manifests:
            await verify_template_boundaries(manifest, where=f"template {slug!r}")

    try:
        asyncio.run(_verify())
    except ValueError as exc:
        typer.echo(f"  {exc}", err=True)
        raise typer.Exit(1) from exc

    async def _run():
        from sqlalchemy import select

        from celine.onboarding.models.rec import Rec

        async with async_session() as db:
            for slug, name, manifest in manifests:
                result = await db.execute(select(Rec).where(Rec.slug == slug))
                existing = result.scalar_one_or_none()
                if existing:
                    existing.name = name
                    existing.manifest = manifest
                    existing.active = True
                else:
                    db.add(Rec(slug=slug, name=name, manifest=manifest, active=True))
                typer.echo(f"  Imported: {slug} ({name})")
            await db.commit()
        typer.echo(f"Done. {len(manifests)} template(s) imported.")

    asyncio.run(_run())

    typer.echo("API will pick up changes automatically.")


def _write_export(content: bytes, output: str) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _data_rows(content: bytes) -> int:
    """Records in an exported CSV: not the leading ``#`` lines, not the column line.

    Parsed rather than counted by line, because a register field can hold a
    newline inside quotes.
    """
    import csv
    import io

    lines = content.decode("utf-8").splitlines(keepends=True)
    while lines and lines[0].startswith("#"):
        lines.pop(0)
    records = [row for row in csv.reader(io.StringIO("".join(lines))) if row]
    return max(len(records) - 1, 0)


@app.command()
def export_csv(
    rec: str = typer.Option(..., "--rec", "-r", help="REC slug"),
    output: str = typer.Option("", "--output", help="Where to write the file"),
    local: bool = _LOCAL,
    api_url: str = _API_URL,
    token: str = _TOKEN,
):
    """Export a community's register — every application, every field — for its own use.

    Through the API, as the console does, so the same authorization and the same
    audit row apply. **Not a way to give data to another organisation**: it names
    no recipient. The supply-point list (`export-pod-list`) is the governed way.
    The file holds personal data: store it encrypted and delete it when done.
    """
    if not output:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output = str(Path(settings.data_dir) / "exports" / rec / f"submissions-{stamp}.csv")

    async def _go():
        transport = build(local, api_url=api_url, token=token)
        try:
            content = await transport.export_csv(rec)
        finally:
            await transport.aclose()
        path = _write_export(content, output)
        typer.echo(f"Exported {_data_rows(content)} submissions to {path}")

    _run(_go())


@app.command()
def export_pod_list(
    rec: str = typer.Option(..., "--rec", "-r", help="REC slug"),
    offer: str = typer.Option(
        ...,
        "--offer",
        help="Offer id the consent must cover. Consent is purpose-scoped: "
        "agreeing to a different offer is not agreeing to this one.",
    ),
    output: str = typer.Option("", "--output", help="Where to write the file"),
    local: bool = _LOCAL,
    api_url: str = _API_URL,
    token: str = _TOKEN,
):
    """Export the community's evidence of which supply points stood authorised, and withdrawn.

    The collector's own dated record under one offer (ADR-0010): kept here,
    handed to nobody, and recorded as a disclosure nowhere. Names, hashes, DIDs
    and evidence bundles stay out: that material lives in the dataspace, where it
    is verifiable and revocable, and a second copy is how two records of the same
    consent start to disagree.

    Through the API, as the console does; a refusal writes nothing. The file is a
    snapshot, and its header states when it was generated.
    """
    if not output:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output = str(Path(settings.data_dir) / "exports" / rec / f"pod-list-{stamp}.csv")

    async def _go():
        transport = build(local, api_url=api_url, token=token)
        try:
            content = await transport.export_pod_list(
                rec,
                offer_id=offer,
            )
        finally:
            await transport.aclose()
        path = _write_export(content, output)
        typer.echo(f"Exported {_data_rows(content)} supply points to {path}")
        typer.echo(
            "Evidence, kept by this community — not a disclosure, and not for handing "
            "over. A snapshot: a later decision is not in it."
        )

    _run(_go())


def _print_sync(report: dict) -> None:
    mode = "DRY RUN — nothing was written" if report["dry_run"] else "sync"
    typer.echo(
        f"{report['rec']} -> registry community {report['community']} "
        f"({mode}; prune={'on' if report['prune'] else 'off'})"
    )
    setup = report["setup"]
    colour = {
        "succeeded": typer.colors.GREEN,
        "failed": typer.colors.RED,
        "skipped": typer.colors.YELLOW,
    }.get(setup["status"])
    line = f"set up community: {setup['status']}"
    if setup.get("reason"):
        line += f" — {setup['reason']}"
    elif setup["status"] == "succeeded":
        line += f" (members {setup.get('members')}, accounts created {setup.get('created')})"
    typer.secho(line, fg=colour)

    for kind in ("nodes", "areas"):
        typer.echo(f"{kind.upper():<8} {'KEY':<30} {'BOUNDARY':<14} OUTCOME")
        for item in report[kind]:
            note = item.get("reason") or ""
            if item.get("renamed_from"):
                renamed = f"renamed {item['renamed_from']} -> {item['key']}"
                note = renamed + (f"; {note}" if note else "")
            if item.get("members") is not None:
                note = f"members {item['members']}" + (f"; {note}" if note else "")
            colour = {"refused": typer.colors.RED, "not_run": typer.colors.RED}.get(item["outcome"])
            typer.secho(
                f"{'':<8} {item['key'][:30]:<30} {(item.get('boundary_id') or '-'):<14} "
                f"{item['outcome']}" + (f"  {note}" if note else ""),
                fg=colour,
            )


@app.command()
def registry_sync(
    rec: str = typer.Option(..., "--rec", "-r", help="REC slug"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Write nothing, anywhere; print what a sync would do"
    ),
    prune: bool = typer.Option(
        False,
        "--prune",
        help="Also delete registry areas the template no longer declares "
        "(refused for an area that still has members)",
    ),
    local: bool = _LOCAL,
    api_url: str = _API_URL,
    token: str = typer.Option(
        None,
        "--token",
        help="A realm admin's own access token. Required unless --local: no service "
        "account may start a sync",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON"),
):
    """Push a REC's template areas to its registry community, as a realm admin.

    The same route the console calls, `POST /api/admin/recs/{rec}/registry-sync`,
    authorised as `recs.write`, which only a realm-level `admins` group holds.
    Pass that admin's own token with `--token`, or run `--local` in process under
    the break-glass rules (ALLOW_LOCAL_ADMIN=true). The CLI's own
    client-credentials identity is never used for it.

    Exits 1 when any area or node was refused, or the community set-up failed.
    """
    if not local and not token:
        typer.secho(
            "registry-sync needs --token <a realm admin's access token>, or --local. "
            "It is a person's decision: the CLI's service account cannot start one.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)

    async def _go():
        transport = build(local, api_url=api_url, token=token)
        try:
            report = await transport.registry_sync(rec, dry_run=dry_run, prune=prune)
        finally:
            await transport.aclose()
        if as_json:
            import json

            typer.echo(json.dumps(report, indent=2))
        else:
            _print_sync(report)
        if not report["ok"] or report["setup"]["status"] == "failed":
            raise typer.Exit(1)

    _run(_go())


@app.command()
def check_offers(
    rec: str = typer.Option(..., "--rec", "-r", help="REC slug"),
):
    """Report stored consents whose offer ids no longer validate.

    Submissions taken before the ids were checked at capture, or taken against a
    vocabulary that has since changed, can hold an offer this community no longer
    offers — or one that is disclosed rather than consented. The connector refuses
    both at provisioning, and the refusal reads as ``share_provisioned = false``,
    which looks exactly like somebody choosing not to share.

    Read-only. It names what to ask again for; it does not decide, because an
    unusable consent cannot be repaired from this side — only re-asked.
    """
    from sqlalchemy import select

    from celine.onboarding.models.database import async_session
    from celine.onboarding.models.submission import Submission
    from celine.onboarding.services import submission_service

    async def _run():
        await _load_recs()
        async with async_session() as db:
            rows = (
                (
                    await db.execute(
                        select(Submission)
                        .where(Submission.rec_slug == rec)
                        .where(Submission.data_sharing_consent.is_(True))
                        .order_by(Submission.created_at.asc())
                    )
                )
                .scalars()
                .all()
            )

            bad = 0
            for sub in rows:
                ids = list(sub.data_sharing_consent_offer_ids or [])
                if not ids:
                    bad += 1
                    typer.echo(f"  {sub.ref}  consent recorded, no offers named")
                    continue
                try:
                    await submission_service._validate_sharing_offer_ids(rec, ids)
                except ValueError as exc:
                    bad += 1
                    typer.echo(f"  {sub.ref}  {exc}")

            typer.echo(
                f"{len(rows)} consent(s) in '{rec}': {len(rows) - bad} valid, {bad} to re-ask."
            )

    asyncio.run(_run())


if __name__ == "__main__":
    app()
