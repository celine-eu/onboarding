"""The documents a participant uploaded, for the operator reviewing them.

The public endpoints are session-gated with a ten-minute TTL, which is right for
the wizard and useless for review — an operator arrives days later. These are
gated by capability instead.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from celine.sdk.auth import JwtUser
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from celine.onboarding.api.admin.deps import ActorDep, DbDep, IpDep, RecDep, require
from celine.onboarding.api.admin.submissions import _owned_submission
from celine.onboarding.models.schemas import DocumentRead
from celine.onboarding.security.policy import Capability
from celine.onboarding.services import audit_service, document_service

router = APIRouter(tags=["admin"])

ReadDep = Annotated[JwtUser, Depends(require(Capability.SUBMISSIONS_READ))]


@router.get(
    "/{rec_slug}/submissions/{submission_id}/documents",
    response_model=list[DocumentRead],
)
async def list_documents(
    submission_id: uuid.UUID,
    _: ReadDep,
    db: DbDep,
    rec_slug: RecDep,
):
    await _owned_submission(db, submission_id, rec_slug)
    return await document_service.list_documents(db, submission_id)


@router.get("/{rec_slug}/submissions/{submission_id}/documents/{document_id}")
async def download_document(
    submission_id: uuid.UUID,
    document_id: uuid.UUID,
    _: ReadDep,
    actor: ActorDep,
    ip: IpDep,
    db: DbDep,
    rec_slug: RecDep,
):
    """Stream one document, decrypted.

    Audited, unlike the document *list*: the bill itself carries the address,
    supply point and consumption history, so opening one is an act worth
    attributing. Listing filenames is not.

    Ownership is checked on both the submission and the document — a document id
    from another community must not be reachable by pairing it with a submission
    id from this one.
    """
    await _owned_submission(db, submission_id, rec_slug)

    document = await document_service.get_document(db, document_id)
    if not document or document.submission_id != submission_id:
        raise HTTPException(404, "Document not found")

    try:
        content = document_service.read_file(document)
    except FileNotFoundError:
        # The row outlives the file if a purge was interrupted. Say so rather than
        # returning a 500 that reads like a bug in the console.
        raise HTTPException(410, "The stored file is no longer available")

    await audit_service.record_and_commit(
        db,
        action="download_document",
        entity_type="document",
        entity_id=str(document_id),
        actor=actor,
        rec_slug=rec_slug,
        ip=ip,
        detail=f"submission={submission_id} type={document.doc_type.value}",
    )

    return Response(
        content=content,
        media_type=document.mime_type,
        headers={"Content-Disposition": (f'attachment; filename="{document.original_filename}"')},
    )
