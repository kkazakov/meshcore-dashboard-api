# Meshcore Dashboard

A self-hosted dashboard for managing and monitoring local [MeshCore](https://github.com/ripplebiz/MeshCore) devices.

## Goal

Provide a comprehensive local interface for MeshCore mesh radio networks, including:

- **Device statistics** — battery, uptime, radio metrics, packet counters
- **Charts and graphs** — historical telemetry visualized over time
- **Chat history** — view and browse messages from the mesh network

All data is stored locally in ClickHouse and exposed through a FastAPI REST API, with the frontend to follow.

## Stack

- **API** — Python / FastAPI
- **Database** — ClickHouse
- **Device connectivity** — MeshCore library (BLE / Serial / TCP)

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edit with your values
```

## Running

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

API docs available at `http://localhost:8000/docs`.

## Configuration

All settings are in `.env`. Key variables:

| Variable | Default | Description |
|---|---|---|
| `CONNECTION_TYPE` | `ble` | Device transport: `ble`, `serial`, or `tcp` |
| `CLICKHOUSE_HOST` | `localhost` | ClickHouse host |
| `CLICKHOUSE_PORT` | `8123` | ClickHouse HTTP port |
| `CLICKHOUSE_DATABASE` | `meshcore` | Database name |

See `.env.example` for the full list.

## Development

```bash
pytest          # run all tests
pytest -v tests/test_status.py::test_status_ok   # run a single test
ruff check .    # lint
ruff format .   # format
```
