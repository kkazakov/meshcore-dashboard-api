# AGENTS.md — Meshcore Dashboard

Guidelines for agentic coding agents working in this repo.

---

## Project Overview

Python FastAPI server that:
- Receives telemetry from MeshCore radio mesh devices (BLE / Serial / TCP).
- Stores all data in ClickHouse.
- Exposes a REST API for dashboards and integrations.

---

## Directory Structure

```
app/
  main.py              # FastAPI app factory, lifespan, router registration, root logging
  config.py            # Pydantic-settings Settings singleton (reads .env)
  events.py            # WebSocket event bus (queue, broadcast, 1s debounce)
  api/
    deps.py            # require_token dependency (60s in-memory token cache)
    routes/            # One file per resource (status.py, auth.py, messages.py, …)
  db/
    clickhouse.py      # get_client(), ping() — new client per call
  meshcore/
    connection.py      # Global asyncio.Lock (device_lock) — serialize all device access
    channel_cache.py   # 12-hour in-process channel list cache
    telemetry_common.py, telemetry.py, telemetry_json.py
  workers/             # Background pollers (started in lifespan)
    message_poller.py
    repeater_telemetry_poller.py
tests/                 # pytest files mirroring app/ structure
docker-aio/clickhouse/initdb.d/00_init.sql  # Schema: users, tokens, messages, repeaters, repeater_telemetry
requirements.txt
.env / .env.example
```

---

## Setup & Running

```bash
# Setup
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # edit with real values

# Run server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

API docs at `http://localhost:8000/docs`.

---

## Build / Lint / Test Commands

```bash
# Run all tests
pytest

# Run a single test file
pytest tests/test_status.py

# Run a single test by name
pytest tests/test_status.py::test_status_ok

# Verbose output
pytest -v

# Lint (ruff — preferred linter, replaces flake8 + isort)
ruff check .

# Check formatting without modifying
ruff format --check .

# Auto-format
ruff format .

# Type-check
mypy app/
```

No `pyproject.toml` or `ruff.toml`; ruff and pytest run with CLI defaults.

---

## Code Style

### Language & Version
- Python 3.11+. Use modern syntax: `match`, `X | None` instead of `Optional[X]`, `tuple[bool, float]` instead of `Tuple`.

### Formatting
- **ruff** is the formatter and linter (no separate black/flake8 config).
- Line length: **88** characters (ruff default).
- Double quotes for strings.

### Imports
- Order: standard library → third-party → local; each group separated by a blank line.
- Absolute imports only: `from app.db.clickhouse import get_client` — never `from ..db`.
- No wildcard imports (`from module import *`).

### Naming Conventions
| Kind | Convention | Example |
|---|---|---|
| Modules / packages | `snake_case` | `telemetry_common.py` |
| Classes | `PascalCase` | `StatusResponse` |
| Functions / variables | `snake_case` | `get_client()` |
| Module-level constants | `_UPPER_SNAKE_CASE` | `_MAX_CHANNEL_SLOTS = 8` |
| Public constants | `UPPER_SNAKE_CASE` | `MAX_RETRIES = 3` |
| Pydantic models | `PascalCase` | `ClickhouseHealth` |
| Unused parameters | prefix `_` | `_email: str` |

### Type Annotations
- All function signatures must be fully annotated (parameters + return type).
- Use `pydantic.BaseModel` for all API request/response schemas; define models in the same file as their route.
- Use `pydantic-settings BaseSettings` for configuration. Never call `os.getenv` outside `app/config.py`.
- Note: `app/meshcore/telemetry_common.py` predates these rules and uses `os.getenv` + `load_dotenv` directly — do not copy that pattern.

### Route File Structure
```python
"""Module docstring describing endpoints, auth requirements, request/response."""

import logging
# … other imports …

logger = logging.getLogger(__name__)
router = APIRouter()

# Pydantic schemas for this resource
class MyRequest(BaseModel): ...
class MyResponse(BaseModel): ...

# Route handlers
@router.get("/api/resource")
def list_resource(_email: str = Depends(require_token)) -> list[MyResponse]:
    ...
```

### Error Handling
- Never swallow exceptions silently; at minimum log with `logger.error(...)`.
- Use `except SpecificException` — avoid bare `except:`.
- Route handlers raise `HTTPException` for client errors:
  ```python
  raise HTTPException(status_code=404, detail={"status": "error", "message": "not found"})
  ```
- ClickHouse / IO failures: catch in `app/db/`, return a typed result or raise a domain exception.

### Logging
- Per-module logger: `logger = logging.getLogger(__name__)`
- Root logger configured **only** in `app/main.py`. Never call `logging.basicConfig` elsewhere.
- Levels: `DEBUG` verbose · `INFO` normal ops · `WARNING` recoverable · `ERROR` failures.

### Async Rules
- Route functions are **synchronous by default**; use `async def` only when actually `await`-ing.
- All MeshCore calls are async; wrap in `async def` and use `async with device_lock:` from `app/meshcore/connection.py`.
- Always disconnect in a `finally` block: `await asyncio.wait_for(meshcore.disconnect(), timeout=5)`.
- ClickHouse is synchronous; call via `await asyncio.to_thread(...)` from async contexts.

### Configuration
- All config in `app/config.py` as a `pydantic-settings` `Settings` class.
- Access via singleton: `from app.config import settings`.
- Never hardcode hosts, ports, credentials, or feature flags outside `app/config.py`.

---

## Testing Guidelines

- Tests live in `tests/`, one file per `app/` module (e.g. `tests/test_messages.py`).
- Use `fastapi.testclient.TestClient` at module level for synchronous route tests.
- Mock **all** external dependencies with `unittest.mock.patch` / `MagicMock` / `AsyncMock`.
- Never hit real ClickHouse or real radio devices in unit tests.
- Descriptive test names: `test_send_message_channel_not_found_returns_404`.
- Use `@contextmanager` helper functions for reusable auth mocking (see `_valid_token()` pattern).
- Always assert both status code **and** response body content on error paths.
- Async tests use `@pytest.mark.asyncio` with `async def` inside a test class.
- Use `pytest.fixture(autouse=True)` for per-test setup/teardown (e.g. resetting caches).

---

## MeshCore Connectivity

- Device-connection helpers: `app/meshcore/telemetry_common.py`.
- Supported transports: **BLE**, **Serial**, **TCP** — set via `connection_type` in settings.
- Serialize all device access with `async with device_lock:` (`app/meshcore/connection.py`).
- Always call `await asyncio.wait_for(meshcore.disconnect(), timeout=5)` in a `finally` block.
- Key functions: `connect_to_device`, `find_contact_by_name`, `find_contact_by_public_key`, `get_status`, `status_to_dict`.

---

## ClickHouse

- Client wrapper: `app/db/clickhouse.py`.
- `get_client()` returns a **new** `clickhouse_connect.Client` per call (not thread-safe).
- `ping()` returns `tuple[bool, float]` — used by `GET /status`.
- Store timestamps as `DateTime64(3, 'UTC')`.
- Schema source of truth: `docker-aio/clickhouse/initdb.d/00_init.sql`.

---

## WebSocket Real-Time Broadcasting

- Endpoint: `ws://localhost:8000/ws`
- Authenticate: `{"type": "auth", "token": "<api-token>"}` → `{"type": "welcome", "email": "user@example.com"}`
- Broadcasts: `{"type": "new_message", "data": {...}}`
- Server debounces 1 second and batches up to 100 messages per broadcast.

---

## Adding a New Endpoint

1. Create `app/api/routes/<resource>.py` with an `APIRouter` and Pydantic models.
2. Register the router in `app/main.py` with `app.include_router(router, tags=["resource"])`.
3. Add tests in `tests/test_<resource>.py` following the existing mock patterns.
