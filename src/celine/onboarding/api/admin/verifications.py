"""The REC's verification of a participant, recorded before approval.

Approval refuses until one is recorded (`services/review.py`). Recording needs the
same capability as approving, `submissions.review`: vouching for a person is part
of the decision, not a data correction an `editors` operator can make.

An offline check with evidence (`offline-with-evidence`, REQ-0042) is recorded
with the digests of the evidence files, never the files: the console sends the
file to `.../verifications/evidence`, which hashes it in memory and **stores
nothing of it** (a browser on a plain-http host has no WebCrypto to hash it
itself); `onboarding-cli` hashes it on the operator's machine and sends the
digest. The file stays with the community, which keeps its evidence anyway —
this service does not want members' identity documents.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Annotated

from celine.sdk.auth import JwtUser
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from celine.onboarding.api.admin.deps import ActorDep, DbDep, IpDep, RecDep, require
from celine.onboarding.api.admin.submissions import _owned_submission
from celine.onboarding.models.schemas import VerificationCreate, VerificationRead
from celine.onboarding.models.verification import VerificationMethod
from celine.onboarding.security.policy import Capability
from celine.onboarding.services import verification

router = APIRouter(tags=["admin"])

ReadDep = Annotated[JwtUser, Depends(require(Capability.SUBMISSIONS_READ))]
ReviewDep = Annotated[JwtUser, Depends(require(Capability.SUBMISSIONS_REVIEW))]


@router.get(
    "/{rec_slug}/submissions/{submission_id}/verifications",
    response_model=list[VerificationRead],
)
async def list_verifications(submission_id: uuid.UUID, _: ReadDep, db: DbDep, rec_slug: RecDep):
    """Every verification recorded, oldest first. The last is in force."""
    submission = await _owned_submission(db, submission_id, rec_slug)
    return submission.verifications


@router.post(
    "/{rec_slug}/submissions/{submission_id}/verifications",
    response_model=VerificationRead,
    status_code=201,
)
async def record_verification(
    submission_id: uuid.UUID,
    body: VerificationCreate,
    _: ReviewDep,
    actor: ActorDep,
    ip: IpDep,
    db: DbDep,
    rec_slug: RecDep,
):
    """Record how the REC verified the person; supersedes any earlier one.

    409 once the submission is rejected (or still a draft); 422 for a document that
    is missing, not on this submission, or given with `offline`; for a plain
    `offline` where an evidence digest is required (REQ-0042); and for anything
    but a verification with evidence after approval.
    """
    submission = await _owned_submission(db, submission_id, rec_slug)
    _assert_recordable(submission)
    try:
        return await verification.record(
            db,
            submission,
            method=body.method,
            document_id=body.document_id,
            evidence=[e.model_dump() for e in body.evidence] if body.evidence else None,
            note=body.note,
            actor=actor,
            ip=ip,
            rec_slug=rec_slug,
        )
    except verification.VerificationError as exc:
        raise HTTPException(422, str(exc)) from exc


def _assert_recordable(submission) -> None:
    if submission.status not in verification.RECORDABLE:
        raise HTTPException(
            409,
            f"A verification can be recorded only while a submission is submitted, "
            f"under review or approved, not {submission.status.value}",
        )


_CHUNK = 1024 * 1024


async def _sha256(file: UploadFile) -> str:
    """The file's sha256, read in chunks. Nothing is written anywhere."""
    digest = hashlib.sha256()
    while chunk := await file.read(_CHUNK):
        digest.update(chunk)
    return digest.hexdigest()


@router.post(
    "/{rec_slug}/submissions/{submission_id}/verifications/evidence",
    response_model=VerificationRead,
    status_code=201,
)
async def record_verification_with_evidence(
    submission_id: uuid.UUID,
    _: ReviewDep,
    actor: ActorDep,
    ip: IpDep,
    db: DbDep,
    rec_slug: RecDep,
    files: Annotated[list[UploadFile], File(description="The evidence the community keeps")],
    kinds: Annotated[
        list[str], Form(description="One per file: utility_bill, id_document or other")
    ],
    note: Annotated[str | None, Form(max_length=1000)] = None,
):
    """Record an offline check with the evidence it rested on (`offline-with-evidence`).

    Each file is hashed and discarded; only `{kind, sha256}` is recorded.
    """
    submission = await _owned_submission(db, submission_id, rec_slug)
    _assert_recordable(submission)
    if len(files) != len(kinds):
        raise HTTPException(422, "Give one kind per evidence file")
    evidence = [
        {"kind": kind, "sha256": await _sha256(file)}
        for file, kind in zip(files, kinds, strict=True)
    ]
    try:
        return await verification.record(
            db,
            submission,
            method=VerificationMethod.OFFLINE_WITH_EVIDENCE,
            evidence=evidence,
            note=note,
            actor=actor,
            ip=ip,
            rec_slug=rec_slug,
        )
    except verification.VerificationError as exc:
        raise HTTPException(422, str(exc)) from exc
