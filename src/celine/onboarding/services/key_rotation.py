"""Re-encrypt everything at rest with the first `ENCRYPTION_KEY` key.

The second half of a key rotation (services/crypto.py): with the new key first in
`ENCRYPTION_KEY` and the old one after it, every encrypted column and every
stored document is rewritten under the new key, and a value stored unencrypted
(written while no key was set) is encrypted. Afterwards the old key can be
dropped. Idempotent: a value already under the first key is left alone.

A value no configured key opens is counted and left as it is, never overwritten:
the run reports it, and the old key must not be dropped until it is resolved.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Text, select, type_coerce, update
from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.config.settings import settings
from celine.onboarding.models import Base, Document
from celine.onboarding.models.encrypted import EncryptedJSON, EncryptedString
from celine.onboarding.services import crypto

logger = logging.getLogger(__name__)


@dataclass
class RotationReport:
    rewritten: dict[str, int] = field(default_factory=dict)
    unchanged: int = 0
    undecryptable: list[str] = field(default_factory=list)
    missing_files: int = 0

    @property
    def ok(self) -> bool:
        return not self.undecryptable


def encrypted_columns() -> list[tuple]:
    """Every (table, column) whose type encrypts."""
    return [
        (table, column)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, EncryptedString | EncryptedJSON)
    ]


async def rotate_columns(db: AsyncSession, report: RotationReport) -> None:
    for table, column in encrypted_columns():
        pk = list(table.primary_key.columns)
        raw = type_coerce(column, Text)
        rows = (await db.execute(select(*pk, raw).where(column.is_not(None)))).all()
        where_name = f"{table.name}.{column.name}"
        for row in rows:
            *keys, value = row
            try:
                new = crypto.rotate(value.encode("utf-8"))
            except crypto.DecryptionError:
                report.undecryptable.append(f"{where_name} {dict(zip([c.name for c in pk], keys))}")
                continue
            if new is None:
                report.unchanged += 1
                continue
            condition = [c == k for c, k in zip(pk, keys, strict=True)]
            await db.execute(
                update(table)
                .where(*condition)
                .values({column.name: type_coerce(new.decode("utf-8"), Text)})
            )
            report.rewritten[where_name] = report.rewritten.get(where_name, 0) + 1
        await db.commit()


async def rotate_documents(db: AsyncSession, report: RotationReport) -> None:
    documents = (await db.execute(select(Document.id, Document.file_path))).all()
    for document_id, relative in documents:
        path = Path(settings.data_dir) / relative
        if not path.is_file():
            report.missing_files += 1
            continue
        try:
            new = crypto.rotate(path.read_bytes())
        except crypto.DecryptionError:
            report.undecryptable.append(f"document {document_id}")
            continue
        if new is None:
            report.unchanged += 1
            continue
        # Replaced, not rewritten in place: a crash mid-write must not leave a
        # truncated file that no key opens.
        tmp = path.with_name(path.name + ".rotating")
        tmp.write_bytes(new)
        os.replace(tmp, path)
        report.rewritten["documents"] = report.rewritten.get("documents", 0) + 1


async def rotate_all(db: AsyncSession) -> RotationReport:
    report = RotationReport()
    await rotate_columns(db, report)
    await rotate_documents(db, report)
    return report
