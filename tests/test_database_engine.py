"""The engine checks a pooled connection before handing it out.

A connection the server or the network closed while it sat idle in the pool would
otherwise fail the first request after an idle stretch. The unit suite has no
Postgres to close one under; `tests/e2e/test_stale_connections_e2e.py` does.
"""

from celine.onboarding.models import database


def test_the_engine_pings_a_pooled_connection_before_use():
    assert database.ENGINE_OPTIONS["pool_pre_ping"] is True
    assert database.engine.sync_engine.pool._pre_ping is True
