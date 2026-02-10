"""
GET /api/telemetry — fetch live telemetry from a MeshCore repeater.

Authentication
--------------
Requires a valid ``x-api-token`` header obtained from ``POST /api/login``.

Query parameters (at least one required)
-----------------------------------------
repeater_name : str
    Partial, case-insensitive name of the target contact.
public_key : str
    Full or prefix public key of the target contact.
password : str  (optional)
    Device password for contacts that require login.
"""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.deps import require_token
from app.meshcore import telemetry_common

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

    try:
        try:
            meshcore = await telemetry_common.connect_to_device(config, verbose=False)
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
        return TelemetryResponse(status="ok", data=result)

    finally:
        if meshcore:
            try:
                await asyncio.wait_for(meshcore.disconnect(), timeout=5)
            except Exception:
                pass
