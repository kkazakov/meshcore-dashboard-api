"""
Repeater telemetry poller — background task that fetches telemetry from all enabled
repeaters at a configurable interval and stores the metrics in ClickHouse.

Design
------
- Queries the ``repeaters`` table for all entries where ``enabled = true``.
- For each repeater, fetches live telemetry using the same logic as the /api/telemetry endpoint.
- Extracts battery_voltage and battery_percentage, stores them as key-value rows in repeater_telemetry.
- Uses the global ``device_lock`` to avoid conflicts with API routes.
- Exponential back-off on connection failures.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Sequence

from app.config import settings
from app.db.clickhouse import get_client
from app.meshcore import telemetry_common
from app.meshcore.connection import device_lock

logger = logging.getLogger(__name__)

_BACKOFF_BASE: float = 2.0
_BACKOFF_MAX: float = 60.0


def _get_enabled_repeaters() -> Sequence[tuple[str, str, str, str]]:
    """
    Fetch all enabled repeaters from ClickHouse.
    Returns list of (id, name, public_key, password) tuples.
    """
    client = get_client()
    result = client.query(
        "SELECT id, name, public_key, password "
        "FROM repeaters FINAL "
        "WHERE enabled = true "
        "ORDER BY name"
    )
    return [(str(row[0]), row[1], row[2], row[3] or "") for row in result.result_rows]


def _insert_telemetry_rows(rows: list[dict[str, Any]]) -> None:
    """Insert telemetry rows into ClickHouse (runs in a thread)."""
    if not rows:
        return

    client = get_client()
    data = [
        [
            row["recorded_at"],
            row["repeater_id"],
            row["repeater_name"],
            row["metric_key"],
            row["metric_value"],
        ]
        for row in rows
    ]
    client.insert(
        "repeater_telemetry",
        data,
        column_names=[
            "recorded_at",
            "repeater_id",
            "repeater_name",
            "metric_key",
            "metric_value",
        ],
    )
    logger.info("Inserted %d telemetry row(s) into ClickHouse", len(rows))


async def _fetch_repeater_telemetry(
    repeater_id: str,
    repeater_name: str,
    public_key: str,
    password: str,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Fetch telemetry from a single repeater and return rows for insertion.
    Returns empty list if fetch fails.
    """
    async with device_lock:
        meshcore = None
        try:
            try:
                meshcore = await telemetry_common.connect_to_device(
                    config, verbose=False
                )
            except Exception as exc:
                logger.warning(
                    "Failed to connect to device for repeater %s: %s",
                    repeater_name,
                    exc,
                )
                return []

            contact = await telemetry_common.find_contact_by_public_key(
                meshcore, public_key, verbose=False, debug=False
            )

            if contact is None:
                logger.warning(
                    "Repeater %s not found (public_key=%s)", repeater_name, public_key
                )
                return []

            status_data = await telemetry_common.get_status(
                meshcore, contact, password, verbose=False, max_retries=2
            )

            if status_data is None:
                logger.warning(
                    "No telemetry response from repeater %s — may be offline",
                    repeater_name,
                )
                return []

            recorded_at = datetime.now(timezone.utc)
            rows: list[dict[str, Any]] = []

            bat_mv = status_data.get("bat", 0)
            bat_v = bat_mv / 1000
            bat_pct = telemetry_common.calculate_battery_percentage(bat_mv)

            rows.append(
                {
                    "recorded_at": recorded_at,
                    "repeater_id": repeater_id,
                    "repeater_name": repeater_name,
                    "metric_key": "battery_voltage",
                    "metric_value": round(bat_v, 3),
                }
            )
            rows.append(
                {
                    "recorded_at": recorded_at,
                    "repeater_id": repeater_id,
                    "repeater_name": repeater_name,
                    "metric_key": "battery_percentage",
                    "metric_value": round(bat_pct, 1),
                }
            )

            logger.debug(
                "Fetched telemetry from %s: battery_voltage=%.3fV, battery_percentage=%.1f%%",
                repeater_name,
                bat_v,
                bat_pct,
            )

            return rows

        finally:
            if meshcore is not None:
                try:
                    await asyncio.wait_for(meshcore.disconnect(), timeout=5)
                except Exception:
                    pass


async def _poll_all_repeaters(config: dict[str, Any]) -> int:
    """
    Poll all enabled repeaters and store their telemetry.
    Returns total number of telemetry rows inserted.
    """
    try:
        repeaters = await asyncio.to_thread(_get_enabled_repeaters)
    except Exception as exc:
        logger.error("Failed to fetch enabled repeaters: %s", exc)
        return 0

    if not repeaters:
        logger.debug("No enabled repeaters to poll")
        return 0

    logger.info("Polling %d repeater(s) for telemetry", len(repeaters))

    all_rows: list[dict[str, Any]] = []
    for repeater_id, name, public_key, password in repeaters:
        try:
            rows = await _fetch_repeater_telemetry(
                repeater_id, name, public_key, password, config
            )
            all_rows.extend(rows)
        except Exception as exc:
            logger.error("Error polling repeater %s: %s", name, exc)

    if all_rows:
        await asyncio.to_thread(_insert_telemetry_rows, all_rows)

    return len(all_rows)


async def run_repeater_telemetry_poller() -> None:
    """
    Long-running coroutine: poll all enabled repeaters at ``REPEATER_POLL_INTERVAL`` seconds.

    Intended to be launched as a background task from the FastAPI lifespan and
    cancelled on shutdown.
    """
    poll_interval = float(settings.repeater_poll_interval)
    logger.info("Repeater telemetry poller starting (interval=%ds)", int(poll_interval))
    config = telemetry_common.load_config()
    backoff: float = _BACKOFF_BASE

    while True:
        try:
            count = await _poll_all_repeaters(config)
            if count > 0:
                logger.info("Poll cycle complete: %d telemetry row(s) stored", count)
            backoff = _BACKOFF_BASE
            await asyncio.sleep(poll_interval)

        except asyncio.CancelledError:
            logger.info("Repeater telemetry poller cancelled — shutting down")
            break

        except Exception as exc:
            logger.error("Poll cycle failed: %s — retrying in %.0fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

    logger.info("Repeater telemetry poller stopped")
