"""
Telemetry endpoints.

GET /api/telemetry
    Fetch live telemetry from a MeshCore repeater.
    Requires a valid ``x-api-token`` header obtained from ``POST /api/login``.

GET /api/telemetry/history/{repeater_id}
    Fetch historical telemetry records for a repeater from ClickHouse.
    Requires a valid ``x-api-token`` header obtained from ``POST /api/login``.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.deps import require_token
from app.db.clickhouse import get_client
from app.meshcore import telemetry_common
from app.meshcore.connection import device_lock

logger = logging.getLogger(__name__)

router = APIRouter()


class TelemetryResponse(BaseModel):
    status: str
    data: dict[str, Any] | None = None


@router.get("/api/telemetry", response_model=TelemetryResponse)
async def get_telemetry(
    repeater_name: str | None = Query(
        default=None, description="Partial contact name (case-insensitive)"
    ),
    public_key: str | None = Query(
        default=None, description="Contact public key or prefix"
    ),
    password: str | None = Query(
        default=None, description="Device password (if required)"
    ),
    _email: str = Depends(require_token),
) -> TelemetryResponse:
    """
    Retrieve live telemetry from a MeshCore repeater.

    Provide **either** ``repeater_name`` or ``public_key`` (or both; name is
    tried first).  Returns a JSON envelope:

    ```json
    { "status": "ok", "data": { ... } }
    ```

    Error responses use the same envelope shape with an appropriate HTTP status
    code:

    - **400** — neither query parameter supplied.
    - **401** — invalid or missing ``x-api-token``.
    - **404** — contact not found on the device.
    - **502** — device connection failed.
    - **504** — no telemetry response received from the device (timeout/offline).
    """
    if not repeater_name and not public_key:
        raise HTTPException(
            status_code=400,
            detail={
                "status": "error",
                "message": "Provide repeater_name or public_key",
            },
        )

    config = telemetry_common.load_config()
    meshcore = None

    async with device_lock:
        try:
            try:
                meshcore = await telemetry_common.connect_to_device(
                    config, verbose=False
                )
            except Exception as exc:
                logger.error("Failed to connect to MeshCore device: %s", exc)
                raise HTTPException(
                    status_code=502,
                    detail={
                        "status": "error",
                        "message": f"Device connection failed: {exc}",
                    },
                ) from exc

            # Resolve contact — prefer name, fall back to public key
            contact = None
            if repeater_name:
                contact = await telemetry_common.find_contact_by_name(
                    meshcore, repeater_name, verbose=False, debug=False
                )
            if contact is None and public_key:
                contact = await telemetry_common.find_contact_by_public_key(
                    meshcore, public_key, verbose=False, debug=False
                )

            if contact is None:
                identifier = repeater_name or public_key
                raise HTTPException(
                    status_code=404,
                    detail={
                        "status": "error",
                        "message": f"Contact '{identifier}' not found",
                    },
                )

            status_data = await telemetry_common.get_status(
                meshcore,
                contact,
                password or "",
                verbose=False,
                max_retries=3,
            )

            if status_data is None:
                raise HTTPException(
                    status_code=504,
                    detail={
                        "status": "error",
                        "message": "No status response received — device may be offline or out of range",
                    },
                )

            result = telemetry_common.status_to_dict(
                status_data,
                contact_name=contact["name"],
                public_key=contact["data"].get("public_key"),
            )

            # Attempt to fetch sensor telemetry (temperature, humidity, pressure).
            # This is a separate BinaryReqType.TELEMETRY request.  Not all devices
            # support it, so a None/empty result is treated as non-fatal.
            sensors = await telemetry_common.get_sensor_telemetry(
                meshcore, contact, verbose=False, max_retries=2
            )
            if sensors:
                result["sensors"] = sensors
            else:
                # Include the key with null so clients know it was attempted
                result["sensors"] = None
                if sensors is None:
                    logger.debug(
                        "No sensor telemetry response from %s — device may not support it",
                        contact["name"],
                    )

            return TelemetryResponse(status="ok", data=result)

        finally:
            if meshcore:
                try:
                    await asyncio.wait_for(meshcore.disconnect(), timeout=5)
                except Exception:
                    pass


# ── Telemetry history ─────────────────────────────────────────────────────────


class TelemetryDataPoint(BaseModel):
    date: str
    value: str


class TelemetryHistoryResponse(BaseModel):
    data: dict[str, list[TelemetryDataPoint]]


@router.get(
    "/api/telemetry/history/{repeater_id}",
    response_model=TelemetryHistoryResponse,
)
def get_telemetry_history(
    repeater_id: str,
    from_: str | None = Query(
        default=None,
        alias="from",
        description="Start of the time range (ISO 8601, e.g. 2025-02-10T00:00:00 or 2025-02-10 00:00:00)",
    ),
    to: str | None = Query(
        default=None,
        description="End of the time range (ISO 8601, e.g. 2025-02-14T00:00:00 or 2025-02-14 00:00:00)",
    ),
    keys: str | None = Query(
        default=None,
        description="Comma-separated list of metric keys to return",
    ),
    _email: str = Depends(require_token),
) -> TelemetryHistoryResponse:
    """
    Return historical telemetry for a repeater from ClickHouse.

    Path parameter
    --------------
    repeater_id : UUID string of the repeater.

    Query parameters
    ----------------
    from : datetime (ISO 8601) — inclusive lower bound on ``recorded_at``.
    to   : datetime (ISO 8601) — inclusive upper bound on ``recorded_at`` (defaults to now).
    keys : comma-separated metric keys to include, e.g.
           ``battery_voltage,battery_percentage``.

    Response shape
    --------------
    ```json
    {
      "data": {
        "battery_voltage": [
          {"date": "2026-02-13T05:40:59.072000", "value": "3.65"},
          ...
        ]
      }
    }
    ```
    """
    if not keys or not keys.strip():
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "message": "keys query parameter is required"},
        )

    metric_keys = [k.strip() for k in keys.split(",") if k.strip()]
    if not metric_keys:
        raise HTTPException(
            status_code=400,
            detail={
                "status": "error",
                "message": "keys must contain at least one valid metric key",
            },
        )

    def _parse_dt(value: str) -> datetime:
        """Accept ISO 8601 with either 'T' or space separator."""
        try:
            return datetime.fromisoformat(value.replace(" ", "T")).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail={
                    "status": "error",
                    "message": f"Invalid datetime value: '{value}'. Use ISO 8601 format.",
                },
            )

    from_dt: datetime | None = _parse_dt(from_) if from_ is not None else None
    to_dt: datetime | None = _parse_dt(to) if to is not None else None

    client = get_client()

    query = (
        "SELECT metric_key, recorded_at, metric_value "
        "FROM repeater_telemetry "
        "WHERE repeater_id = {rid:UUID} "
        "  AND metric_key IN {mkeys:Array(String)}"
    )
    params: dict[str, Any] = {
        "rid": repeater_id,
        "mkeys": metric_keys,
    }

    if from_dt is not None:
        query += " AND recorded_at >= {from_dt:DateTime64(3, 'UTC')}"
        params["from_dt"] = from_dt
    if to_dt is not None:
        query += " AND recorded_at <= {to_dt:DateTime64(3, 'UTC')}"
        params["to_dt"] = to_dt
    else:
        query += " AND recorded_at <= now64()"

    query += " ORDER BY metric_key, recorded_at"

    try:
        result = client.query(query, parameters=params)
    except Exception as exc:
        logger.error("ClickHouse query failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={"status": "error", "message": f"Database query failed: {exc}"},
        ) from exc

    data: dict[str, list[TelemetryDataPoint]] = {key: [] for key in metric_keys}
    for row in result.result_rows:
        metric_key, recorded_at, metric_value = row[0], row[1], row[2]
        if metric_key in data:
            data[metric_key].append(
                TelemetryDataPoint(
                    date=recorded_at.isoformat()
                    if hasattr(recorded_at, "isoformat")
                    else str(recorded_at),
                    value=str(metric_value),
                )
            )

    return TelemetryHistoryResponse(data=data)
