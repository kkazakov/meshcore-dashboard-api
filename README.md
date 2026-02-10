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

---

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edit with your values
```

### Database

Apply the schema migrations in order against your ClickHouse instance:

```bash
# 001 — users / authentication
clickhouse-client --multiquery < sql/001_authentication.sql

# 002 — messages store
clickhouse-client --multiquery < sql/002_messages.sql
```

Or via the Python client (adjust credentials as needed):

```python
import clickhouse_connect
client = clickhouse_connect.get_client(host="localhost", username="admin", password="...")
client.command(open("sql/001_authentication.sql").read())
client.command(open("sql/002_messages.sql").read())
```

---

## Running

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Interactive API docs: `http://localhost:8000/docs`

---

## Configuration

All settings are read from `.env` (see `.env.example` for the full template).

### Device connection

| Variable | Default | Description |
|---|---|---|
| `CONNECTION_TYPE` | `ble` | Transport: `ble`, `serial`, or `tcp` |
| `BLE_ADDRESS` | *(empty)* | BLE MAC address — leave blank to auto-scan |
| `BLE_PIN` | *(empty)* | Optional 6-digit BLE pairing PIN |
| `SERIAL_PORT` | `/dev/ttyUSB0` | Serial device path |
| `SERIAL_BAUDRATE` | `115200` | Serial baud rate |
| `TCP_HOST` | `192.168.1.100` | TCP host of the companion device |
| `TCP_PORT` | `4000` | TCP port of the companion device |
| `DEBUG` | `false` | Enable verbose MeshCore debug logging |

### ClickHouse

| Variable | Default | Description |
|---|---|---|
| `CLICKHOUSE_HOST` | `localhost` | ClickHouse host |
| `CLICKHOUSE_PORT` | `8123` | ClickHouse HTTP port |
| `CLICKHOUSE_DATABASE` | `meshcore_dashboard` | Database name |
| `CLICKHOUSE_USER` | `admin` | ClickHouse username |
| `CLICKHOUSE_PASSWORD` | *(empty)* | ClickHouse password |

### API server

| Variable | Default | Description |
|---|---|---|
| `API_HOST` | `0.0.0.0` | Bind address |
| `API_PORT` | `8000` | Listen port |

---

## API Reference

All endpoints that require authentication expect an `x-api-token` header obtained from `POST /api/login`.

### Health

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/status` | — | Server and ClickHouse health check |

**Response**
```json
{ "status": "ok", "clickhouse": { "connected": true, "latency_ms": 1.2 } }
```

---

### Auth

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/api/login` | — | Password login — returns a session token |

**Request**
```json
{ "email": "admin", "password": "secret" }
```

**Response**
```json
{ "token": "abc123…", "email": "admin", "username": "admin", "access_rights": "" }
```

---

### Telemetry

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/api/telemetry` | x-api-token | Fetch live telemetry from a MeshCore repeater |

**Query parameters** (at least one required)

| Parameter | Description |
|---|---|
| `repeater_name` | Partial, case-insensitive contact name |
| `public_key` | Full or prefix public key of the contact |
| `password` | Device password (optional) |

**Response**
```json
{
  "status": "ok",
  "data": {
    "contact_name": "Vardar repeater",
    "battery": { "mv": 3950, "v": 3.95, "percentage": 75.0 },
    "uptime": { "seconds": 86400, "days": 1, "hours": 0, "minutes": 0, "seconds_rem": 0 },
    "radio": { "noise_floor": -90, "last_rssi": -75, "last_snr": 8.5, "tx_queue": 0, "queue_full_events": 0 },
    "packets": { "sent": { "total": 120, "flood": 80, "direct": 40 }, "received": { "total": 95, "flood": 60, "direct": 35 }, "duplicates": { "direct": 2, "flood": 1 } },
    "airtime": { "tx": 320, "rx": 280 },
    "public_key": "df33c12f…",
    "pubkey_prefix": "df33c12f8a4b"
  }
}
```

---

### Messaging — Channels

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/api/channels` | x-api-token | List all initialised channels on the device |
| `POST` | `/api/channels` | x-api-token | Create a new channel on the next free slot |
| `DELETE` | `/api/channels` | x-api-token | Delete a channel by name |

**GET /api/channels — response**
```json
{
  "status": "ok",
  "channels": [
    { "index": 0, "name": "General", "secret_hex": "0a1b2c3d…" },
    { "index": 1, "name": "Admin",   "secret_hex": "ff00aa11…" }
  ]
}
```

**POST /api/channels — request**
```json
{ "name": "MyChannel" }
```
Returns `201` with the full updated channel list.
Returns `409` if a channel with that name already exists.
Returns `400` if all 8 slots are occupied.

**DELETE /api/channels — request**
```json
{ "name": "MyChannel" }
```
Returns `200` with the full updated channel list (deleted channel absent).
Returns `404` if the channel name is not found.

---

## Background Workers

### Message Poller (`app/workers/message_poller.py`)

Runs automatically on server startup as an `asyncio` background task.

**What it does:**
- Maintains a **persistent connection** to the companion device
- Every **2 seconds**, drains the full message queue via `get_msg()` until the device reports no more messages
- Stores each message in the `messages` ClickHouse table
- For **channel messages** (`CHAN`): resolves the channel name from the device; splits the `"SenderName: text"` format into separate `sender_name` and `text` fields
- For **direct messages** (`PRIV`): resolves the sender name from the contacts list using the 6-byte `pubkey_prefix`

**Resilience:**
- On connection failure, reconnects with exponential back-off (2 s → 4 s → … capped at 60 s)
- Channel and contact name caches are refreshed on each new connection

**Messages table columns:**

| Column | Type | Description |
|---|---|---|
| `received_at` | `DateTime64(3, 'UTC')` | Server ingest timestamp |
| `msg_type` | `LowCardinality(String)` | `CHAN` or `PRIV` |
| `channel_idx` | `Int8` | Channel slot (0–7); `-1` for PRIV |
| `channel_name` | `String` | Channel name at ingest time |
| `sender_timestamp` | `UInt32` | Unix timestamp from the sender device |
| `sender_pubkey_prefix` | `String` | 6-byte hex pubkey prefix (PRIV only) |
| `sender_name` | `String` | Resolved contact/sender name |
| `path_len` | `UInt8` | Hop count (0 = direct) |
| `snr` | `Float32` | Signal-to-noise ratio in dB |
| `text` | `String` | Message body |
| `txt_type` | `UInt8` | Protocol type flag (0 = plain, 2 = signed) |
| `signature` | `String` | 4-byte hex signature (PRIV signed only) |

---

## Development

```bash
pytest              # run all tests
pytest -v           # verbose
ruff check .        # lint
ruff format .       # format
mypy app/           # type-check
```
