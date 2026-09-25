"""Exports, streamed rather than left on disk.

`onboarding-cli export-csv` writes into `data/exports/` and leaves it there, which
is fine for a one-off run on a server and wrong for a console: every download
would deposit another copy of the community's personal data next to the last one.
These write to a temporary file, stream it, and delete it.

`onboarding-cli` calls these same routes, so the console and the terminal run one
implementation and write the same audit rows.

Neither is a disclosure. The register export is the community's own copy; the
supply-point list is its own dated evidence under one offer (ADR-0010). Neither
names a recipient, and neither records anything with the dataspace.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from celine.sdk.auth import JwtUser
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from celine.onboarding.api.admin.deps import ActorDep, DbDep, IpDep, RecDep, require
from celine.onboarding.config.settings import settings
from celine.onboarding.outputs.csv_export import export_pod_list, export_submissions_csv
from celine.onboarding.security.policy import Capability
from celine.onboarding.services import audit_service

router = APIRouter(tags=["admin"])

ExportDep = Annotated[JwtUser, Depends(require(Capability.EXPORT))]


class CsvExportRequest(BaseModel):
    """No fields. The register export is for the community's own use and names no
    recipient; a body is still accepted so existing callers sending ``{}`` work."""


class PodListRequest(BaseModel):
    """The offer, and nothing else.

    ``recipient_ref`` was dropped (ADR-0010, amended 2026-09-25): the party the
    offer's consent is read for comes from the offer. A caller still sending it
    is not refused — unknown fields are ignored, like any other — so a body
    written for the old contract keeps working, and names nobody.
    """

    offer_id: str = Field(
        ...,
        description="Consent is purpose-scoped: somebody who agreed to a different "
        "offer has not agreed to this one.",
    )


def _staging_dir() -> Path:
    directory = Path(settings.data_dir) / "exports"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _streamed(path: Path, filename: str) -> FileResponse:
    return FileResponse(
        path,
        media_type="text/csv",
        filename=filename,
        # Unlinked once the response has been written. The file exists only for
        # the length of the request.
        background=BackgroundTask(lambda: path.unlink(missing_ok=True)),
    )


@router.post("/{rec_slug}/exports/csv")
async def export_csv(
    body: CsvExportRequest,
    _: ExportDep,
    actor: ActorDep,
    ip: IpDep,
    db: DbDep,
    rec_slug: RecDep,
):
    """Every submission in this community, as CSV."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    handle = tempfile.NamedTemporaryFile(
        dir=_staging_dir(), prefix=f"{rec_slug}-", suffix=".csv", delete=False
    )
    handle.close()
    path = Path(handle.name)

    try:
        count = await export_submissions_csv(db, path, rec_slug=rec_slug)
    except Exception:
        path.unlink(missing_ok=True)
        raise

    await audit_service.record_and_commit(
        db,
        action="export_csv",
        entity_type="submission",
        entity_id=None,
        actor=actor,
        rec_slug=rec_slug,
        ip=ip,
        detail=f"rows={count}",
    )
    return _streamed(path, f"{rec_slug}-submissions-{stamp}.csv")


@router.post("/{rec_slug}/exports/pod-list")
async def export_pods(
    body: PodListRequest,
    _: ExportDep,
    actor: ActorDep,
    ip: IpDep,
    db: DbDep,
    rec_slug: RecDep,
):
    """The community's dated evidence for one offer, streamed as CSV.

    Kept by the community and recorded as a disclosure nowhere (ADR-0010): which
    supply points stood authorised, and which members had withdrawn, at
    generation time. A snapshot — a later decision is not in it. The file's
    header says so.
    """
    generated_at = datetime.now(UTC)
    stamp = generated_at.strftime("%Y%m%dT%H%M%SZ")
    handle = tempfile.NamedTemporaryFile(
        dir=_staging_dir(), prefix=f"{rec_slug}-pods-", suffix=".csv", delete=False
    )
    handle.close()
    path = Path(handle.name)

    try:
        count = await export_pod_list(
            db,
            path,
            rec_slug=rec_slug,
            offer_id=body.offer_id,
            generated_at=generated_at,
        )
    except ValueError as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(422, str(exc))
    except Exception:
        path.unlink(missing_ok=True)
        raise

    await audit_service.record_and_commit(
        db,
        action="export_pod_list",
        entity_type="submission",
        entity_id=None,
        actor=actor,
        rec_slug=rec_slug,
        ip=ip,
        detail=f"pods={count} offer={body.offer_id}",
    )
    return _streamed(path, f"{rec_slug}-pods-{stamp}.csv")
