import asyncio
from pathlib import Path
from typing import Optional

import typer

from celine.onboarding.config.settings import settings

app = typer.Typer(name="cer", help="CER Onboarding CLI")


@app.command()
def import_templates(
    filter: Optional[str] = typer.Option(None, "--filter", "-f", help="Import only this slug"),
    all: bool = typer.Option(False, "--all", "-a", help="Import all discovered templates"),
    templates_dir: Optional[str] = typer.Option(None, "--templates-dir", help="Override templates directory"),
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
            typer.echo(f"  Warning: slug '{slug}' does not match directory '{subdir.name}', using directory name")
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
            validate_dataspace_block,
            validate_rec_registry_block,
        )

        try:
            validate_dataspace_block(manifest.get("dataspace"), where=str(manifest_path))
            validate_rec_registry_block(
                manifest.get("rec_registry"), where=str(manifest_path)
            )
        except ValueError as exc:
            typer.echo(f"  {exc}", err=True)
            raise typer.Exit(1) from exc

        manifests.append((slug, name, manifest))

    if not manifests:
        typer.echo("No templates found to import.")
        raise typer.Exit(1)

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


@app.command()
def export_csv(
    output: str = "",
    rec: Optional[str] = typer.Option(None, "--rec", "-r", help="Filter by REC slug"),
    recipient: Optional[str] = typer.Option(
        None,
        "--recipient",
        help="Recipient of this disclosure (org alias/DID/DPA ref). "
        "Naming one records a DataDisclosed provenance event.",
    ),
    purpose: Optional[str] = typer.Option(
        None, "--purpose", help="Comma-separated purpose slugs for the disclosure"
    ),
    agreement_ref: Optional[str] = typer.Option(
        None, "--agreement-ref", help="DPA / agreement reference (never its contents)"
    ),
):
    """Export onboarding submissions to CSV.

    Pass --recipient to record the export as a DataDisclosed provenance event
    (codes and hashes only, never PII).
    """
    from celine.onboarding.models.database import async_session
    from celine.onboarding.outputs.csv_export import export_submissions_csv

    if not output:
        suffix = rec or "all"
        output = str(Path(settings.data_dir) / "exports" / suffix / "submissions.csv")

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    purposes = [p.strip() for p in (purpose or "").split(",") if p.strip()]

    async def _run():
        async with async_session() as db:
            count = await export_submissions_csv(
                db,
                output,
                rec_slug=rec,
                recipient_ref=recipient,
                purpose=purposes,
                agreement_ref=agreement_ref,
            )
            typer.echo(f"Exported {count} submissions to {output}")
            if recipient:
                typer.echo(f"Recorded DataDisclosed to '{recipient}'")

    asyncio.run(_run())


@app.command()
def export_pod_list(
    rec: str = typer.Option(..., "--rec", "-r", help="REC slug"),
    offer: str = typer.Option(
        ...,
        "--offer",
        help="Offer id the consent must cover. Consent is purpose-scoped: "
        "agreeing to a different offer is not agreeing to this handover.",
    ),
    recipient: str = typer.Option(
        ...,
        "--recipient",
        help="Who receives the list (org alias/DID/DPA ref). Recorded as a "
        "DataDisclosed provenance event.",
    ),
    output: str = "",
    purpose: Optional[str] = typer.Option(
        None, "--purpose", help="Comma-separated purpose slugs for the disclosure"
    ),
    agreement_ref: Optional[str] = typer.Option(
        None, "--agreement-ref", help="DPA / agreement reference (never its contents)"
    ),
):
    """Export the supply points whose owners agreed — and nothing else.

    For handing a distributor the PODs it may release. Names, hashes, DIDs and
    evidence stay out: that material lives in the dataspace, where it is
    verifiable and revocable, and a second copy is how two records of the same
    consent start to disagree.

    The file is a snapshot, so the re-export cadence is the revocation latency.
    Re-run it on a schedule; the header states when it was generated.
    """
    from datetime import datetime, timezone

    from celine.onboarding.models.database import async_session
    from celine.onboarding.outputs.csv_export import export_pod_list as _export

    generated_at = datetime.now(timezone.utc)
    if not output:
        stamp = generated_at.strftime("%Y%m%dT%H%M%SZ")
        output = str(
            Path(settings.data_dir) / "exports" / rec / f"pod-list-{stamp}.csv"
        )

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    purposes = [p.strip() for p in (purpose or "").split(",") if p.strip()]

    async def _run():
        async with async_session() as db:
            count = await _export(
                db,
                output,
                rec_slug=rec,
                offer_id=offer,
                recipient_ref=recipient,
                generated_at=generated_at,
                purpose=purposes,
                agreement_ref=agreement_ref,
            )
            typer.echo(f"Exported {count} supply points to {output}")
            typer.echo(f"Recorded DataDisclosed to '{recipient}'")
            typer.echo(
                "This list is a snapshot — consent can be withdrawn, so re-export "
                "on your agreed cadence."
            )

    asyncio.run(_run())


@app.command()
def upload_gdrive(folder_id: str = typer.Option(..., help="Google Drive folder ID")):
    """Upload documents to Google Drive."""
    typer.echo(f"Uploading to folder {folder_id}... (not yet implemented)")


if __name__ == "__main__":
    app()
