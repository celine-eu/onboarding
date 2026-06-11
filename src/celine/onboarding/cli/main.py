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
):
    """Export onboarding submissions to CSV."""
    from celine.onboarding.models.database import async_session
    from celine.onboarding.outputs.csv_export import export_submissions_csv

    if not output:
        suffix = rec or "all"
        output = str(Path(settings.data_dir) / "exports" / suffix / "submissions.csv")

    Path(output).parent.mkdir(parents=True, exist_ok=True)

    async def _run():
        async with async_session() as db:
            count = await export_submissions_csv(db, output, rec_slug=rec)
            typer.echo(f"Exported {count} submissions to {output}")

    asyncio.run(_run())


@app.command()
def upload_gdrive(folder_id: str = typer.Option(..., help="Google Drive folder ID")):
    """Upload documents to Google Drive."""
    typer.echo(f"Uploading to folder {folder_id}... (not yet implemented)")


if __name__ == "__main__":
    app()
