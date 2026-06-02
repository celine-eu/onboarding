import asyncio

import typer

from celine.onboarding.config.settings import settings

app = typer.Typer(name="cer", help="CER Onboarding CLI")


@app.command()
def export_csv(output: str = ""):
    """Export onboarding submissions to CSV."""
    from pathlib import Path

    from celine.onboarding.models.database import async_session
    from celine.onboarding.outputs.csv_export import export_submissions_csv

    if not output:
        output = str(Path(settings.data_dir) / "exports" / "submissions.csv")

    Path(output).parent.mkdir(parents=True, exist_ok=True)

    async def _run():
        async with async_session() as db:
            count = await export_submissions_csv(db, output)
            typer.echo(f"Exported {count} submissions to {output}")

    asyncio.run(_run())


@app.command()
def upload_gdrive(folder_id: str = typer.Option(..., help="Google Drive folder ID")):
    """Upload documents to Google Drive."""
    typer.echo(f"Uploading to folder {folder_id}... (not yet implemented)")


if __name__ == "__main__":
    app()
