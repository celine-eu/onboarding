import io
import zipfile

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.models.database import get_db
from celine.onboarding.services.crypto import validate_download_token

router = APIRouter(prefix="/downloads", tags=["downloads"])


@router.get("/{token}")
async def download_submission_package(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    submission_id = validate_download_token(token)
    if submission_id is None:
        raise HTTPException(403, "Invalid or expired download link")

    from celine.onboarding.services.submission_service import get_submission

    submission = await get_submission(db, submission_id)
    if submission is None:
        raise HTTPException(404, "Submission not found")

    from celine.onboarding.services.document_service import read_file
    from celine.onboarding.services.pdf_service import generate_submission_pdf

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        pdf_bytes = generate_submission_pdf(submission)
        zf.writestr(f"{submission.ref}-summary.pdf", pdf_bytes)

        for doc in submission.documents or []:
            try:
                content = read_file(doc)
                zf.writestr(doc.original_filename, content)
            except Exception:
                pass

    buf.seek(0)
    filename = f"{submission.ref}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
