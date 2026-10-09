"""A pooled connection closed while idle must not fail the next request.

On a deployment, the first request after an idle stretch answered 500 with
asyncpg's `ConnectionDoesNotExistError`: the server or something on the network
path had closed the pooled connection, and the pool handed it out unchecked. The
next request succeeded. The unit suite has no database, so it can only pin the
engine options (`tests/test_database_engine.py`); this reproduces the failure.
"""

from __future__ import annotations

import asyncio
import time

import asyncpg
import httpx
import pytest

from celine.onboarding.services.template_service import CACHE_TTL

from .conftest import E2E_DATABASE_URL

pytestmark = pytest.mark.e2e


def _terminate_other_backends() -> int:
    """Close every other connection to the e2e database, as a dropped link would."""

    async def terminate() -> int:
        conn = await asyncpg.connect(E2E_DATABASE_URL.replace("+asyncpg", ""))
        try:
            return await conn.fetchval(
                "SELECT count(*) FROM ("
                "  SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                "  WHERE datname = current_database() AND pid <> pg_backend_pid()"
                ") AS terminated"
            )
        finally:
            await conn.close()

    return asyncio.run(terminate())


def test_a_request_after_the_pooled_connection_was_closed_succeeds(client: httpx.Client):
    # Warm the pool: this request leaves a connection checked in.
    assert client.get("/api/recs").status_code == 200

    assert _terminate_other_backends() >= 1

    # `/api/recs` answers from the manifest cache for CACHE_TTL seconds; past it,
    # the request reads the database through the pool.
    time.sleep(CACHE_TTL + 0.5)

    response = client.get("/api/recs")
    assert response.status_code == 200, response.text
