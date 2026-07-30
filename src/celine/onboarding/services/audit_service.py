"""Writing the admin audit trail.

Two things this module exists to get right.

**The actor is not optional.** Every write names who did it, so the trail can
answer "who approved this person" rather than only "somebody with the token did".
`Actor` is the one way to say it, which is what keeps the four `actor_*` columns
consistent — a caller cannot set `actor_email` without setting `actor_type`.

**Staged, not committed.** `record` adds the row to the caller's session and
returns. The audit row and the change it describes then commit together, so
neither can exist without the other. The previous helper called `commit()` itself,
one statement after the mutation had already committed — a crash in between left a
change nobody was recorded as having made. Use `record_and_commit` only where
there is no mutation to ride along with (a read that must be logged).
"""

from __future__ import annotations

import getpass
import socket
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from celine.onboarding.models.audit_log import AuditLog


@dataclass(frozen=True)
class Actor:
    """Who performed an audited action.

    See `ACTOR_TYPES` in `models/audit_log.py` for what each type means.
    """

    type: str
    sub: str | None = None
    email: str | None = None
    client_id: str | None = None

    @classmethod
    def from_user(cls, user) -> Actor:
        """An operator or a service, told apart by the shape of their token.

        A `client_credentials` token has a `client_id`/`azp` and no email; an
        operator has both a `sub` and, in this realm, an email. Recording the
        client id for a service is what makes a machine-driven approval
        distinguishable from a human one in the trail.
        """
        claims = getattr(user, "claims", None) or {}
        client_id = claims.get("client_id") or claims.get("azp")

        if user.is_service_account:
            return cls(
                type="service",
                sub=user.sub,
                email=None,
                client_id=str(client_id) if client_id else None,
            )
        return cls(
            type="user",
            sub=user.sub,
            email=user.email,
            # Kept for a user too: it says which client the operator came
            # through, which distinguishes a console action from a CLI one made
            # with the same identity.
            client_id=str(client_id) if client_id else None,
        )

    @classmethod
    def local_cli(cls) -> Actor:
        """`onboarding-cli --local`, the break-glass path with no token at all.

        There is no verified identity here — the authority is shell access to the
        database. Recording the OS user and host is the most that can honestly be
        claimed, and it is deliberately not a `sub`-shaped value that might be
        mistaken for one.
        """
        try:
            who = getpass.getuser()
        except Exception:  # pragma: no cover - no passwd entry (some containers)
            who = "unknown"
        return cls(type="cli", sub=f"{who}@{socket.gethostname()}")

    @classmethod
    def system(cls, reason: str) -> Actor:
        """The platform acting without a caller — a scheduled retry, a listener."""
        return cls(type="system", sub=reason)

    @classmethod
    def shared_token(cls) -> Actor:
        """The pre-authorization era: whoever held `ADMIN_TOKEN`.

        Deleted with the token itself. It exists so that rows written in the
        meantime say "unattributable by construction" rather than looking like an
        actor lookup that failed.
        """
        return cls(type="token")


def record(
    db: AsyncSession,
    *,
    action: str,
    entity_type: str,
    entity_id: str | None,
    actor: Actor,
    rec_slug: str | None,
    ip: str | None = None,
    detail: str | None = None,
) -> AuditLog:
    """Stage an audit row in the caller's transaction. Does **not** commit."""
    entry = AuditLog(
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        rec_slug=rec_slug,
        ip_address=ip,
        detail=detail,
        actor_type=actor.type,
        actor_sub=actor.sub,
        actor_email=actor.email,
        actor_client_id=actor.client_id,
    )
    db.add(entry)
    return entry


async def record_and_commit(
    db: AsyncSession,
    *,
    action: str,
    entity_type: str,
    entity_id: str | None,
    actor: Actor,
    rec_slug: str | None,
    ip: str | None = None,
    detail: str | None = None,
) -> AuditLog:
    """Stage and commit, for audited actions that change nothing themselves.

    A read worth recording — listing a queue, revealing a fiscal code — has no
    mutation to commit alongside.
    """
    entry = record(
        db,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor=actor,
        rec_slug=rec_slug,
        ip=ip,
        detail=detail,
    )
    await db.commit()
    return entry
