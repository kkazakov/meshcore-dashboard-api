"""
Repeater monitoring CRUD endpoints.

Authentication
--------------
All endpoints require a valid ``x-api-token`` header obtained from ``POST /api/login``.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import require_token
from app.db.clickhouse import get_client

logger = logging.getLogger(__name__)

router = APIRouter()


class RepeaterCreate(BaseModel):
    name: str
    public_key: str
    password: str | None = None


class RepeaterUpdate(BaseModel):
    name: str | None = None
    public_key: str | None = None


class RepeaterListItem(BaseModel):
    id: str
    name: str
    public_key: str
    enabled: bool
    created_at: str


class RepeaterListResponse(BaseModel):
    status: str
    repeaters: list[RepeaterListItem]


class RepeaterSingleResponse(BaseModel):
    status: str
    repeater: RepeaterListItem


@router.get("/api/repeaters", response_model=RepeaterListResponse)
def list_repeaters(_email: str = Depends(require_token)) -> RepeaterListResponse:
    """
    List all monitored repeaters.
    """
    client = get_client()
    result = client.query(
        "SELECT id, name, public_key, enabled, created_at "
        "FROM repeaters FINAL "
        "ORDER BY created_at DESC"
    )

    repeaters = []
    for row in result.result_rows:
        repeaters.append(
            RepeaterListItem(
                id=str(row[0]),
                name=row[1],
                public_key=row[2],
                enabled=row[3],
                created_at=row[4].isoformat() if row[4] else "",
            )
        )

    return RepeaterListResponse(status="ok", repeaters=repeaters)


@router.post("/api/repeaters", response_model=RepeaterSingleResponse, status_code=201)
def add_repeater(
    data: RepeaterCreate, _email: str = Depends(require_token)
) -> RepeaterSingleResponse:
    """
    Add a repeater to be monitored.

    Required: name, public_key
    Optional: password
    """
    if not data.name or not data.name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    if not data.public_key or not data.public_key.strip():
        raise HTTPException(status_code=400, detail="public_key is required")

    client = get_client()

    existing = client.query(
        "SELECT id FROM repeaters FINAL WHERE public_key = {pk:String}",
        parameters={"pk": data.public_key.strip()},
    )
    if existing.result_rows:
        raise HTTPException(
            status_code=409, detail="A repeater with this public_key already exists"
        )

    repeater_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    client.insert(
        "repeaters",
        [
            [
                repeater_id,
                data.name.strip(),
                data.public_key.strip(),
                data.password or "",
                True,
                now,
            ]
        ],
        column_names=["id", "name", "public_key", "password", "enabled", "created_at"],
    )

    logger.info("Added repeater %s (%s)", data.name, repeater_id)

    return RepeaterSingleResponse(
        status="ok",
        repeater=RepeaterListItem(
            id=str(repeater_id),
            name=data.name.strip(),
            public_key=data.public_key.strip(),
            enabled=True,
            created_at=now.isoformat(),
        ),
    )


@router.patch("/api/repeaters/{repeater_id}", response_model=RepeaterSingleResponse)
def update_repeater(
    repeater_id: str,
    data: RepeaterUpdate,
    _email: str = Depends(require_token),
) -> RepeaterSingleResponse:
    """
    Update a repeater's name and/or public_key.

    At least one of ``name`` or ``public_key`` must be provided.
    If ``public_key`` is changed it must not already belong to another repeater.
    """
    try:
        uuid.UUID(repeater_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid repeater ID format")

    if data.name is not None and not data.name.strip():
        raise HTTPException(status_code=400, detail="name must not be blank")
    if data.public_key is not None and not data.public_key.strip():
        raise HTTPException(status_code=400, detail="public_key must not be blank")
    if data.name is None and data.public_key is None:
        raise HTTPException(
            status_code=400,
            detail="At least one of name or public_key must be provided",
        )

    client = get_client()

    existing = client.query(
        "SELECT id, name, public_key, password, enabled FROM repeaters FINAL "
        "WHERE id = {rid:UUID}",
        parameters={"rid": repeater_id},
    )
    if not existing.result_rows:
        raise HTTPException(status_code=404, detail="Repeater not found")

    row = existing.result_rows[0]
    current_name: str = row[1]
    current_public_key: str = row[2]
    password: str = row[3]
    enabled: bool = row[4]

    new_name = data.name.strip() if data.name is not None else current_name
    new_public_key = (
        data.public_key.strip() if data.public_key is not None else current_public_key
    )

    if new_public_key != current_public_key:
        conflict = client.query(
            "SELECT id FROM repeaters FINAL WHERE public_key = {pk:String}",
            parameters={"pk": new_public_key},
        )
        if conflict.result_rows:
            raise HTTPException(
                status_code=409,
                detail="A repeater with this public_key already exists",
            )

    now = datetime.now(timezone.utc)

    client.insert(
        "repeaters",
        [[repeater_id, new_name, new_public_key, password, enabled, now]],
        column_names=["id", "name", "public_key", "password", "enabled", "created_at"],
    )

    logger.info("Updated repeater %s", repeater_id)

    return RepeaterSingleResponse(
        status="ok",
        repeater=RepeaterListItem(
            id=repeater_id,
            name=new_name,
            public_key=new_public_key,
            enabled=enabled,
            created_at=now.isoformat(),
        ),
    )


@router.delete("/api/repeaters/{repeater_id}", response_model=dict[str, Any])
def delete_repeater(
    repeater_id: str, _email: str = Depends(require_token)
) -> dict[str, Any]:
    """
    Delete a repeater by ID.
    """
    try:
        uuid.UUID(repeater_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid repeater ID format")

    client = get_client()

    existing = client.query(
        "SELECT id FROM repeaters FINAL WHERE id = {rid:UUID}",
        parameters={"rid": repeater_id},
    )
    if not existing.result_rows:
        raise HTTPException(status_code=404, detail="Repeater not found")

    client.command(
        "ALTER TABLE repeaters DELETE WHERE id = {rid:UUID}",
        parameters={"rid": repeater_id},
    )

    logger.info("Deleted repeater %s", repeater_id)

    return {"status": "ok", "message": "Repeater deleted"}


@router.post("/api/repeaters/{repeater_id}/enable", response_model=dict[str, Any])
def enable_repeater(
    repeater_id: str, _email: str = Depends(require_token)
) -> dict[str, Any]:
    """
    Enable monitoring for a repeater.
    """
    try:
        uuid.UUID(repeater_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid repeater ID format")

    client = get_client()

    existing = client.query(
        "SELECT id, name, public_key, password FROM repeaters FINAL WHERE id = {rid:UUID}",
        parameters={"rid": repeater_id},
    )
    if not existing.result_rows:
        raise HTTPException(status_code=404, detail="Repeater not found")

    now = datetime.now(timezone.utc)
    row = existing.result_rows[0]
    name, public_key, password = row[1], row[2], row[3]

    client.insert(
        "repeaters",
        [[repeater_id, name, public_key, password, True, now]],
        column_names=["id", "name", "public_key", "password", "enabled", "created_at"],
    )

    logger.info("Enabled repeater %s", repeater_id)

    return {"status": "ok", "message": "Repeater enabled"}


@router.post("/api/repeaters/{repeater_id}/disable", response_model=dict[str, Any])
def disable_repeater(
    repeater_id: str, _email: str = Depends(require_token)
) -> dict[str, Any]:
    """
    Disable monitoring for a repeater.
    """
    try:
        uuid.UUID(repeater_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid repeater ID format")

    client = get_client()

    existing = client.query(
        "SELECT id, name, public_key, password FROM repeaters FINAL WHERE id = {rid:UUID}",
        parameters={"rid": repeater_id},
    )
    if not existing.result_rows:
        raise HTTPException(status_code=404, detail="Repeater not found")

    now = datetime.now(timezone.utc)
    row = existing.result_rows[0]
    name, public_key, password = row[1], row[2], row[3]

    client.insert(
        "repeaters",
        [[repeater_id, name, public_key, password, False, now]],
        column_names=["id", "name", "public_key", "password", "enabled", "created_at"],
    )

    logger.info("Disabled repeater %s", repeater_id)

    return {"status": "ok", "message": "Repeater disabled"}
