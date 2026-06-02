from pathlib import Path


async def upload_to_gdrive(file_path: Path, folder_id: str, filename: str | None = None) -> str:
    """Upload a file to Google Drive. Returns the file ID.

    Requires GOOGLE_APPLICATION_CREDENTIALS env var pointing to service account key.
    """
    raise NotImplementedError(
        "Google Drive upload not yet implemented. "
        "Set GOOGLE_APPLICATION_CREDENTIALS and install google-api-python-client."
    )
