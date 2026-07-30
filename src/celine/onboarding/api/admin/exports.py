"""Exports, streamed rather than left on disk.

`onboarding-cli export-csv` writes into `data/exports/` and leaves it there, which
is fine for a one-off run on a server and wrong for a console: every download
would deposit another copy of the community's personal data next to the last one.
These write to a temporary file, stream it, and delete it.

The provenance emission is unchanged and deliberately so — naming a recipient is
what makes an export a *disclosure*, and that record belongs in ds-provenance
regardless of which door the export came through.
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
    recipient_ref: str | None = Field(
        None,
        description="Who the data is being disclosed to. Naming one records a "
        "DataDisclosed provenance event; omit it for an internal dump.",
    )
    purpose: list[str] = []
    agreement_ref: str | None = None


class PodListRequest(BaseModel):
    offer_id: str = Field(
        ...,
        description="Consent is purpose-scoped: somebody who agreed to a different "
        "offer has not agreed to this handover.",
    )
    recipient_ref: str
    purpose: list[str] = []
    agreement_ref: str | None = None


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
        count = await export_submissions_csv(
            db,
            path,
            rec_slug=rec_slug,
            recipient_ref=body.recipient_ref,
            purpose=body.purpose,
            agreement_ref=body.agreement_ref,
        )
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
        detail=f"rows={count} recipient={body.recipient_ref or '-'}",
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
    """The supply points whose owners agreed, and nothing else.

    A snapshot: somebody who withdraws stays on the recipient's copy until the
    next run, so the re-export cadence *is* the revocation latency. The file's
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
            recipient_ref=body.recipient_ref,
            generated_at=generated_at,
            purpose=body.purpose,
            agreement_ref=body.agreement_ref,
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
        detail=f"pods={count} offer={body.offer_id} recipient={body.recipient_ref}",
    )
    return _streamed(path, f"{rec_slug}-pods-{stamp}.csv")
