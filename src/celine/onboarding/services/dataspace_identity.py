from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from celine.onboarding.config.settings import settings
from celine.onboarding.models.submission import Submission

_SAFE_SUBJECT = re.compile(r"^[A-Za-z0-9._+-]{1,128}$")


def _email_subject_id(email: str | None) -> str:
    normalized = (email or "").strip().lower()
    if not normalized:
        raise ValueError("Cannot build dataspace subject id from email: value is empty")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"email-{digest}"


def _subject_id(submission: Submission) -> str:
    source = settings.dataspace_subject_source.strip().lower()
    if source in {"submission_ref", "ref"}:
        value = submission.ref
    elif source in {"email_hash", "email"}:
        value = _email_subject_id(submission.email)
    else:
        raise ValueError(
            "Unsupported DATASPACE_SUBJECT_SOURCE. Use email_hash or submission_ref."
        )

    subject_id = value.strip().lower()
    if not subject_id:
        raise ValueError(f"Cannot build dataspace subject id from {source}: value is empty")
    if not _SAFE_SUBJECT.fullmatch(subject_id):
        raise ValueError(
            "Dataspace subject id may contain only letters, digits, dot, underscore, "
            "plus and hyphen"
        )
    return subject_id


def _add_optional_arg(command: list[str], flag: str, value: str) -> None:
    if value:
        command.extend([flag, value])


def _parse_generated_at(value: Any) -> datetime:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


async def provision_user_identity(submission: Submission) -> None:
    if not settings.dataspace_vc_enabled:
        return
    if submission.dataspace_vc_id:
        return

    repo_dir = settings.resolve_path(settings.dataspace_repo_dir)
    issuer_script = repo_dir / "scripts" / "credential_issuer.py"
    if not issuer_script.exists():
        raise ValueError(f"Dataspace credential issuer not found: {issuer_script}")

    subject_id = _subject_id(submission)
    command = [
        settings.dataspace_python_bin,
        str(issuer_script),
        "issue-user",
        "--profile",
        settings.dataspace_vc_profile,
        "--subject-id",
        subject_id,
        "--role",
        settings.dataspace_user_role,
        "--ttl-days",
        str(settings.dataspace_vc_ttl_days),
    ]

    _add_optional_arg(command, "--env-file", settings.dataspace_env_file)
    _add_optional_arg(command, "--credentials-dir", settings.dataspace_credentials_dir)
    _add_optional_arg(command, "--status-list-path", settings.dataspace_status_list_path)
    _add_optional_arg(command, "--status-list-url", settings.dataspace_status_list_url)
    _add_optional_arg(command, "--did-documents-dir", settings.dataspace_did_documents_dir)
    _add_optional_arg(
        command,
        "--user-profile-endpoint",
        settings.dataspace_user_profile_endpoint,
    )
    _add_optional_arg(command, "--issuer-did", settings.dataspace_issuer_did)
    _add_optional_arg(command, "--trust-anchor-key", settings.dataspace_trust_anchor_key)
    _add_optional_arg(command, "--users-did-prefix", settings.dataspace_users_did_prefix)
    _add_optional_arg(
        command,
        "--linked-participant-did",
        settings.dataspace_linked_participant_did,
    )

    for action in settings.dataspace_allowed_actions.split(","):
        action = action.strip()
        if action:
            command.extend(["--allowed-action", action])

    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=Path(repo_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        detail = stderr.decode().strip() or stdout.decode().strip()
        raise ValueError(f"Dataspace VC issuance failed: {detail}")

    try:
        evidence = json.loads(stdout.decode())
    except json.JSONDecodeError as exc:
        raise ValueError("Dataspace VC issuer returned invalid JSON") from exc

    submission.dataspace_subject_id = subject_id
    submission.dataspace_did = evidence.get("subjectDid")
    submission.dataspace_vc_id = evidence.get("credentialId")
    submission.dataspace_vc_issued_at = _parse_generated_at(evidence.get("generatedAt"))

    if not submission.dataspace_did or not submission.dataspace_vc_id:
        raise ValueError("Dataspace VC issuer response is missing subjectDid or credentialId")
