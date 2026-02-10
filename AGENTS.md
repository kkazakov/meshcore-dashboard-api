# AGENTS.md — Meshcore Dashboard

Guidelines for agentic coding agents (and human contributors) working in this repo.

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
  main.py              # FastAPI app factory & router registration
  config.py            # Pydantic-settings config (reads .env)
  api/
    routes/            # One file per resource (status.py, contacts.py, …)
  db/
    clickhouse.py      # ClickHouse client wrapper (clickhouse-connect)
  meshcore/            # MeshCore connectivity helpers (copied from temp-meshcore/)
    telemetry_common.py
    telemetry.py
    telemetry_json.py
tests/                 # pytest test files mirroring app/ structure
requirements.txt
.env / .env.example
```

---

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit .env with real values
```

---

## Running the Server

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Interactive API docs available at `http://localhost:8000/docs`.

---

## Build / Lint / Test Commands

### Run all tests
```bash
pytest
```

### Run a single test file
```bash
pytest tests/test_status.py
```

### Run a single test by name
```bash
pytest tests/test_status.py::test_status_ok
```

### Run tests with verbose output
```bash
pytest -v
```

### Lint (ruff — preferred)
```bash
ruff check .
ruff format --check .
```

### Format
```bash
ruff format .
```

### Type-check
```bash
mypy app/
```

---

## Code Style

### Language & version
- Python 3.11+. Use modern syntax (`match`, `|` unions, `X | None` instead of `Optional[X]`).

### Formatting
- **ruff** is the formatter and linter (replaces black + flake8 + isort).
- Line length: **88** characters.
- Double quotes for strings.

### Imports
- Standard library → third-party → local; each group separated by a blank line (ruff/isort enforces this).
- Absolute imports only (`from app.db.clickhouse import ping`, never relative `from ..db`).
- Never use wildcard imports (`from module import *`).

### Naming conventions
| Kind | Convention | Example |
|---|---|---|
| Modules / packages | `snake_case` | `telemetry_common.py` |
| Classes | `PascalCase` | `StatusResponse` |
| Functions / variables | `snake_case` | `get_client()` |
| Constants | `UPPER_SNAKE_CASE` | `MAX_RETRIES = 3` |
| Pydantic models | `PascalCase` | `ClickhouseHealth` |

### Type annotations
- All function signatures must be fully annotated (parameters + return type).
- Use `pydantic.BaseModel` for all API request/response schemas.
- Use `pydantic-settings BaseSettings` for configuration (never raw `os.getenv` in application code — only in `app/config.py`).

### Error handling
- Never swallow exceptions silently. At minimum, log with `logger.error(...)`.
- Use `try / except SpecificException` — avoid bare `except:`.
- FastAPI route handlers should raise `fastapi.HTTPException` for client errors.
- ClickHouse / IO failures should be caught in `app/db/` and return a typed result or raise a domain exception; routes translate these into HTTP responses.

### Logging
- Use the stdlib `logging` module. Obtain a logger per module:
  ```python
  import logging
  logger = logging.getLogger(__name__)
  ```
- Root logger is configured once in `app/main.py`. Never call `logging.basicConfig` elsewhere.
- Log levels: `DEBUG` for verbose diagnostics, `INFO` for normal operations, `WARNING` for recoverable issues, `ERROR` for failures.

### Async
- FastAPI route functions are **synchronous by default** unless actual async I/O is performed.
- Use `async def` only when calling `await`-able code (e.g., MeshCore BLE/TCP operations).
- ClickHouse queries via `clickhouse-connect` are synchronous; wrap in `asyncio.to_thread` if called from an async context.

### Configuration
- All config lives in `app/config.py` as a `pydantic-settings` `Settings` class.
- Access via the singleton: `from app.config import settings`.
- Never hardcode hostnames, ports, credentials, or feature flags outside of `app/config.py`.

---

## Testing Guidelines

- Tests live in `tests/`, mirroring the `app/` structure.
- Use `fastapi.testclient.TestClient` for synchronous route tests.
- Mock external dependencies (`ping`, MeshCore connections) with `unittest.mock.patch`.
- Do **not** hit real ClickHouse or real radio devices in unit tests.
- Integration tests (if added) should be in `tests/integration/` and skipped by default (`pytest -m "not integration"`).
- Each test function name starts with `test_` and is descriptive: `test_status_degraded_when_clickhouse_unavailable`.

---

## MeshCore Connectivity

- All device-connection logic lives in `app/meshcore/telemetry_common.py`.
- Supported transports: **BLE**, **Serial**, **TCP** — set via `CONNECTION_TYPE` env var.
- Always call `await meshcore.disconnect()` in a `finally` block after connecting.
- Key functions: `connect_to_device`, `find_contact_by_name`, `get_status`, `status_to_dict`.
- The `meshcore` library is async; all MeshCore calls must be in `async def` functions.

---

## ClickHouse

- Client wrapper: `app/db/clickhouse.py`.
- `get_client()` returns a cached `clickhouse_connect.Client` (HTTP port **8123** by default).
- `ping()` returns `(ok: bool, latency_ms: float)` — used by `GET /status`.
- All schema / table DDL goes in `sql/`.
- Use native ClickHouse types; store timestamps as `DateTime64(3, 'UTC')`.

---

## Adding a New Endpoint

1. Create `app/api/routes/<resource>.py` with an `APIRouter`.
2. Define Pydantic response/request models in the same file (or a `app/models/` file if shared).
3. Register the router in `app/main.py` with `app.include_router(...)`.
4. Add tests in `tests/test_<resource>.py`.
