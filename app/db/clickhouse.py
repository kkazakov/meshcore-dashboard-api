"""
ClickHouse client — thin wrapper around clickhouse-connect.

Usage
-----
    from app.db.clickhouse import get_client, ping

    client = get_client()       # returns a new ClickHouseClient (thread-safe)
    ok, latency_ms = ping()     # health-check used by GET /status
"""

import time
import logging

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from app.config import settings

logger = logging.getLogger(__name__)


def get_client() -> Client:
    """
    Return a new ClickHouse client for the current request/thread.

    ``clickhouse-connect`` clients are not thread-safe — a single cached
    instance shared across threads causes "concurrent queries within the same
    session" errors under load.  Creating a client per call is cheap because
    the underlying HTTP connection pool (urllib3) is managed globally by the
    library and reused automatically.
    """
    return clickhouse_connect.get_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        database=settings.clickhouse_database,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
    )


def ping() -> tuple[bool, float]:
    """
    Send a lightweight ``SELECT 1`` to verify ClickHouse connectivity.

    Returns
    -------
    (ok, latency_ms)
        ok         – True if the server responded correctly
        latency_ms – round-trip time in milliseconds
    """
    start = time.perf_counter()
    try:
        client = get_client()
        result = client.query("SELECT 1")
        ok = result.result_rows == [(1,)]
        latency_ms = (time.perf_counter() - start) * 1000
        return ok, round(latency_ms, 2)
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        logger.error("ClickHouse ping failed: %s", exc)
        return False, round(latency_ms, 2)
