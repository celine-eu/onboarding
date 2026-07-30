"""How `onboarding-cli` reaches the review and enablement flow.

Two backends behind one interface, so every command is written once.

**HTTP is the default, and that is the point.** The CLI calls the same
`/api/admin/**` endpoints the console does, with a service-account token, so it
goes through the same OPA decisions and writes the same audit rows. A CLI that
reached into the database would be a second implementation of the rules — and the
one nobody tests against.

**`--local` is the break-glass.** A deployment with no Keycloak still has an
operator with a shell and a `DATABASE_URL`, and that is a better trust boundary
than a shared secret in an env file. It goes through the same
`services/{review,enablement}` layer the API handlers use, so the state machine
and the enablement pipeline cannot diverge between the two — only the
authorization does, because there is no identity to authorize. Every action is
recorded as `actor_type=cli` with the OS user and host.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

import httpx

from celine.onboarding.config.settings import settings


class CliError(RuntimeError):
    """Something the operator can act on — printed without a traceback."""


class Transport(Protocol):
    async def whoami(self) -> dict[str, Any]: ...

    async def list_submissions(
        self, rec: str, *, status: str | None, ref: str | None, limit: int
    ) -> tuple[list[dict], int]: ...

    async def get_submission(
        self, rec: str, submission_id: str, *, reveal: bool = False
    ) -> dict: ...

    async def transition(
        self, rec: str, submission_id: str, target: str, reason: str | None
    ) -> dict: ...

    async def enablement(self, rec: str, submission_id: str) -> dict: ...

    async def retry(self, rec: str, submission_id: str, step: str | None) -> dict: ...

    async def revoke(self, rec: str, submission_id: str) -> dict: ...

    async def purge(self, rec: str, submission_id: str) -> None: ...

    async def audit(
        self, rec: str, *, limit: int, action: str | None, actor: str | None
    ) -> list[dict]: ...

    async def aclose(self) -> None: ...


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class ApiTransport:
    """The real API, authenticated as `svc-onboarding-cli`."""

    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self._base_url = (base_url or settings.onboarding_api_url).rstrip("/")
        self._static_token = token
        self._provider = None
        self._client = httpx.AsyncClient(timeout=60)

    async def _headers(self) -> dict[str, str]:
        if self._static_token:
            return {"Authorization": f"Bearer {self._static_token}"}

        if self._provider is None:
            from celine.sdk.auth import OidcClientCredentialsProvider

            if not settings.oidc_base_url:
                raise CliError(
                    "OIDC_BASE_URL is not set, so the CLI cannot obtain a token.\n"
                    "Set it, pass --token, or use --local for a deployment with no "
                    "Keycloak."
                )
            self._provider = OidcClientCredentialsProvider(
                base_url=settings.oidc_base_url,
                client_id=settings.onboarding_cli_client_id,
                client_secret=settings.onboarding_cli_client_secret,
            )
        token = await self._provider.get_token()
        return {"Authorization": f"Bearer {token.access_token}"}

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        response = await self._client.request(
            method, f"{self._base_url}{path}", headers=await self._headers(), **kwargs
        )
        if response.status_code == 401:
            raise CliError(
                "The API rejected the CLI's token (401). Check "
                "ONBOARDING_CLI_CLIENT_SECRET and that svc-onboarding-cli exists in "
                "Keycloak."
            )
        if response.status_code == 403:
            raise CliError(f"Not permitted: {_detail(response)}")
        if response.status_code == 404:
            raise CliError(f"Not found: {_detail(response)}")
        if response.status_code >= 400:
            raise CliError(f"API error {response.status_code}: {_detail(response)}")
        return response

    async def whoami(self) -> dict[str, Any]:
        return (await self._request("GET", "/api/admin/me")).json()

    async def list_submissions(self, rec, *, status, ref, limit):
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if ref:
            params["ref"] = ref
        response = await self._request("GET", f"/api/admin/{rec}/submissions", params=params)
        total = int(response.headers.get("X-Total-Count", 0))
        return response.json(), total

    async def get_submission(self, rec, submission_id, *, reveal=False):
        return (
            await self._request(
                "GET",
                f"/api/admin/{rec}/submissions/{submission_id}",
                params={"reveal": str(reveal).lower()},
            )
        ).json()

    async def transition(self, rec, submission_id, target, reason):
        body: dict[str, Any] = {"target": target}
        if reason:
            body["reason"] = reason
        return (
            await self._request(
                "POST",
                f"/api/admin/{rec}/submissions/{submission_id}/transition",
                json=body,
            )
        ).json()

    async def enablement(self, rec, submission_id):
        return (
            await self._request("GET", f"/api/admin/{rec}/submissions/{submission_id}/enablement")
        ).json()

    async def retry(self, rec, submission_id, step):
        return (
            await self._request(
                "POST",
                f"/api/admin/{rec}/submissions/{submission_id}/enablement/retry",
                json={"step": step},
            )
        ).json()

    async def revoke(self, rec, submission_id):
        return (
            await self._request(
                "POST",
                f"/api/admin/{rec}/submissions/{submission_id}/enablement/revoke",
            )
        ).json()

    async def purge(self, rec, submission_id) -> None:
        await self._request("DELETE", f"/api/admin/{rec}/submissions/{submission_id}")

    async def audit(self, rec, *, limit, action, actor):
        rows = (
            await self._request("GET", f"/api/admin/{rec}/audit-logs", params={"limit": limit})
        ).json()
        # Filtered client-side: the endpoint paginates by time, and adding query
        # parameters for it would be an API change made for one caller.
        if action:
            rows = [r for r in rows if r["action"] == action]
        if actor:
            rows = [
                r
                for r in rows
                if actor in (r.get("actor_sub") or "") or actor in (r.get("actor_email") or "")
            ]
        return rows

    async def aclose(self) -> None:
        await self._client.aclose()


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        return body.get("detail") or response.text
    except Exception:
        return response.text


# ---------------------------------------------------------------------------
# Direct
# ---------------------------------------------------------------------------


class LocalTransport:
    """Straight to the database, through the same service layer as the API."""

    def __init__(self) -> None:
        if not settings.allow_local_admin:
            raise CliError(
                "--local bypasses authorization entirely and is off by default.\n"
                "Set ALLOW_LOCAL_ADMIN=true to use it. It is the break-glass for a "
                "deployment with no Keycloak; anything else should go through the "
                "API so the action is authorized and attributed."
            )
        from celine.onboarding.services.audit_service import Actor

        self._actor = Actor.local_cli()

    async def _session(self):
        from celine.onboarding.models.database import async_session
        from celine.onboarding.services import template_service

        await template_service.load_recs_from_db()
        return async_session()

    async def whoami(self) -> dict[str, Any]:
        from celine.onboarding.security.policy import ALL_CAPABILITIES
        from celine.onboarding.services import template_service

        await template_service.load_recs_from_db()
        return {
            "sub": self._actor.sub,
            "email": None,
            "subject_type": "cli",
            "organizations": [],
            "realm_groups": [],
            # --local has no policy to consult: the authority is database access.
            # Reporting the full set is honest about that rather than implying a
            # check that did not happen.
            "recs": [
                {
                    "slug": slug,
                    "name": template_service.load_manifest(slug).get("name", slug),
                    "organization": template_service.organization_for(slug) or None,
                    "capabilities": sorted(c.value for c in ALL_CAPABILITIES),
                }
                for slug in sorted(template_service.get_slugs())
            ],
        }

    async def _serialise(self, submission, *, reveal: bool = False) -> dict:
        from celine.onboarding.api.admin.submissions import _read

        return _read(submission, reveal=reveal).model_dump(mode="json")

    async def list_submissions(self, rec, *, status, ref, limit):
        from celine.onboarding.models.submission import SubmissionStatus
        from celine.onboarding.services import submission_service

        target = SubmissionStatus(status) if status else None
        async with await self._session() as db:
            rows = await submission_service.list_submissions(
                db, rec_slug=rec, limit=limit, status=target, ref=ref
            )
            total = await submission_service.count_submissions(
                db, rec_slug=rec, status=target, ref=ref
            )
            return [await self._serialise(r) for r in rows], total

    async def _load(self, db, rec: str, submission_id: str):
        from celine.onboarding.services import submission_service

        submission = await submission_service.get_submission(db, uuid.UUID(submission_id))
        if not submission or submission.rec_slug != rec:
            raise CliError(f"Submission {submission_id} not found in {rec!r}")
        return submission

    async def get_submission(self, rec, submission_id, *, reveal=False):
        async with await self._session() as db:
            submission = await self._load(db, rec, submission_id)
            return await self._serialise(submission, reveal=reveal)

    async def transition(self, rec, submission_id, target, reason):
        from celine.onboarding.models.submission import SubmissionStatus
        from celine.onboarding.services import review
        from celine.onboarding.services.enablement import EnablementFailed
        from celine.onboarding.workflows.engine import InvalidTransition

        async with await self._session() as db:
            submission = await self._load(db, rec, submission_id)
            try:
                result = await review.transition(
                    db,
                    submission,
                    SubmissionStatus(target),
                    actor=self._actor,
                    reason=reason,
                    rec_slug=rec,
                )
            except EnablementFailed as exc:
                raise CliError(str(exc)) from exc
            except (ValueError, InvalidTransition) as exc:
                raise CliError(str(exc)) from exc
            return await self._serialise(result)

    async def _render_enablement(self, submission_id, rows) -> dict:
        from celine.onboarding.api.admin.enablement import _render

        return _render(uuid.UUID(str(submission_id)), rows).model_dump(mode="json")

    async def enablement(self, rec, submission_id):
        from celine.onboarding.services import enablement as service

        async with await self._session() as db:
            submission = await self._load(db, rec, submission_id)
            rows = await service.load_steps(db, submission.id)
            return await self._render_enablement(submission.id, rows)

    async def retry(self, rec, submission_id, step):
        from celine.onboarding.services import enablement as service

        async with await self._session() as db:
            submission = await self._load(db, rec, submission_id)
            try:
                rows = await service.retry(db, submission, step=step)
            except KeyError as exc:
                raise CliError(str(exc)) from exc
            return await self._render_enablement(submission.id, rows)

    async def revoke(self, rec, submission_id):
        from celine.onboarding.services import enablement as service

        async with await self._session() as db:
            submission = await self._load(db, rec, submission_id)
            rows = await service.revoke(db, submission)
            return await self._render_enablement(submission.id, rows)

    async def purge(self, rec, submission_id) -> None:
        from pathlib import Path

        from celine.onboarding.services import audit_service

        async with await self._session() as db:
            submission = await self._load(db, rec, submission_id)
            ref = submission.ref
            for doc in submission.documents:
                (Path(settings.data_dir) / doc.file_path).unlink(missing_ok=True)
            await db.delete(submission)
            await db.commit()
            await audit_service.record_and_commit(
                db,
                action="delete",
                entity_type="submission",
                entity_id=str(submission_id),
                actor=self._actor,
                rec_slug=rec,
                detail=f"ref={ref} — GDPR erasure (local CLI)",
            )

    async def audit(self, rec, *, limit, action, actor):
        from sqlalchemy import select

        from celine.onboarding.models.audit_log import AuditLog
        from celine.onboarding.models.schemas import AuditLogRead

        async with await self._session() as db:
            query = (
                select(AuditLog)
                .where(AuditLog.rec_slug == rec)
                .order_by(AuditLog.created_at.desc())
                .limit(limit)
            )
            if action:
                query = query.where(AuditLog.action == action)
            if actor:
                query = query.where(AuditLog.actor_sub.ilike(f"%{actor}%"))
            rows = (await db.execute(query)).scalars().all()
            return [AuditLogRead.model_validate(r).model_dump(mode="json") for r in rows]

    async def aclose(self) -> None:
        return None


def build(local: bool, *, api_url: str | None, token: str | None) -> Transport:
    return LocalTransport() if local else ApiTransport(api_url, token)
