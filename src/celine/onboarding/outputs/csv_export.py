import csv
import io
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.models.submission import Submission
from celine.onboarding.services.template_service import load_manifest

BASE_FIELDS = [
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


def _extra_field_keys() -> list[str]:
    manifest = load_manifest()
    return [f["key"] for f in manifest.get("fields", {}).get("extra", []) if "key" in f]


async def export_submissions_csv(db: AsyncSession, output_path: str | Path) -> int:
    result = await db.execute(
        select(Submission).order_by(Submission.created_at.desc())
    )
    submissions = result.scalars().all()

    extra_keys = _extra_field_keys()
    fieldnames = BASE_FIELDS + extra_keys

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for sub in submissions:
            row = {field: str(getattr(sub, field, "")) for field in BASE_FIELDS}
            extra = sub.extra_data or {}
            for key in extra_keys:
                row[key] = str(extra.get(key, ""))
            writer.writerow(row)

    return len(submissions)
