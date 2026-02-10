"""
ClickHouse client — thin wrapper around clickhouse-connect.

Usage
-----
    from app.db.clickhouse import get_client, ping

    client = get_client()       # returns a synchronous ClickHouseClient
    ok, latency_ms = await ping()  # health-check used by GET /status
"""

import time
import logging
from functools import lru_cache

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from app.config import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_client() -> Client:
    """
    Return a cached ClickHouse client.

    The client is created once and reused for the lifetime of the process.
    Call `get_client.cache_clear()` in tests to force a new connection.
    """
    logger.info(
        "Connecting to ClickHouse at %s:%s (db=%s)",
        settings.clickhouse_host,
        settings.clickhouse_port,
        settings.clickhouse_database,
    )
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
