import csv
import io
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.models.submission import Submission

FIELDS = [
    "id",
    "status",
    "first_name",
    "last_name",
    "email",
    "phone",
    "fiscal_code",
    "pod_code",
    "gdpr_consent",
    "policy_consent",
    "statute_consent",
    "created_at",
    "updated_at",
]


async def export_submissions_csv(db: AsyncSession, output_path: str | Path) -> int:
    result = await db.execute(
        select(Submission).order_by(Submission.created_at.desc())
    )
    submissions = result.scalars().all()

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for sub in submissions:
            writer.writerow({field: str(getattr(sub, field, "")) for field in FIELDS})

    return len(submissions)
