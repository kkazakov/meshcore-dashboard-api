import asyncio
import logging
import os
import sys
from dotenv import load_dotenv
from meshcore import MeshCore, EventType, BinaryReqType

logger = logging.getLogger(__name__)


def load_config():
    """Load configuration from .env file"""
    load_dotenv()

    config = {
        "connection_type": os.getenv("CONNECTION_TYPE", "ble").lower(),
        "ble_address": os.getenv("BLE_ADDRESS", ""),
        "ble_pin": os.getenv("BLE_PIN", ""),
        "serial_port": os.getenv("SERIAL_PORT", "/dev/ttyUSB0"),
        "serial_baudrate": int(os.getenv("SERIAL_BAUDRATE", "115200")),
        "tcp_host": os.getenv("TCP_HOST", "192.168.1.100"),
        "tcp_port": int(os.getenv("TCP_PORT", "4000")),
        "debug": os.getenv("DEBUG", "false").lower() == "true",
    }

    return config


async def connect_to_device(config, verbose=True):
    """Connect to MeshCore device based on configuration"""
    conn_type = config["connection_type"]
    debug = config["debug"]

    if verbose:
        print(f"Connecting to MeshCore device via {conn_type.upper()}...")

    try:
        if conn_type == "ble":
            ble_address = config["ble_address"] if config["ble_address"] else None
            ble_pin = config["ble_pin"] if config["ble_pin"] else None

            if verbose:
                if ble_address:
                    print(f"Using BLE address: {ble_address}")
                else:
                    print("Scanning for BLE devices...")
                if ble_pin:
                    print(f"Using PIN for pairing: {ble_pin}")

            if ble_pin:
                meshcore = await MeshCore.create_ble(
                    ble_address, pin=ble_pin, debug=debug
                )
            else:
                meshcore = await MeshCore.create_ble(ble_address, debug=debug)

        elif conn_type == "serial":
            if verbose:
                print(
                    f"Using serial port: {config['serial_port']} @ {config['serial_baudrate']} baud"
                )
            meshcore = await MeshCore.create_serial(
                config["serial_port"], config["serial_baudrate"], debug=debug
            )

        elif conn_type == "tcp":
            if verbose:
                print(
                    f"Using TCP connection: {config['tcp_host']}:{config['tcp_port']}"
                )
            meshcore = await MeshCore.create_tcp(
                config["tcp_host"], config["tcp_port"], debug=debug
            )

        else:
            if verbose:
                print(f"ERROR: Unknown connection type: {conn_type}")
                print("Valid types are: ble, serial, tcp")
            raise ValueError(f"Unknown connection type: {conn_type}")

        if verbose:
            print("✓ Connected successfully!\n")
        return meshcore

    except Exception as e:
        if verbose:
            print(f"ERROR: Failed to connect to device: {e}")
        raise


async def login_to_contact(meshcore, contact, password, verbose=True):
    """Login to contact with password"""
    if not password:
        if verbose:
            print("WARNING: No password provided. Status may not be accessible.")
        return True

    if verbose:
        print(f"Logging in to {contact['name']}...")

    def event_handler(event):
        if verbose:
            if event.type == EventType.LOGIN_SUCCESS:
                print("✓ Login successful!")
            elif event.type == EventType.LOGIN_FAILED:
                print(f"✗ Login failed: {event.payload}")

    sub = meshcore.dispatcher.subscribe(None, event_handler, attribute_filters={})

    try:
        result = await meshcore.commands.send_login(contact["id"], password)

        if result is None:
            if verbose:
                print("ERROR: Login request timed out or failed")
            return False

        if result.type == EventType.ERROR:
            if verbose:
                print(f"ERROR: Login request failed: {result.payload}")
            return False

        login_event = await meshcore.dispatcher.wait_for_event(
            EventType.LOGIN_SUCCESS, timeout=10
        )

        if login_event is None:
            failed_event = await meshcore.dispatcher.wait_for_event(
                EventType.LOGIN_FAILED, timeout=1
            )
            if failed_event:
                if verbose:
                    print(f"✗ Login failed: {failed_event.payload}")
                return False
            if verbose:
                print("WARNING: No login response (might not be required)")
            return True

        return True

    except asyncio.TimeoutError:
        if verbose:
            print("WARNING: Login timeout (might not be required)")
        return True
    finally:
        sub.unsubscribe()


async def find_contact_by_name(
    meshcore, name, verbose=True, max_retries=5, debug=False
):
    """Find contact by name (case-insensitive, partial match) with retry logic"""
    if verbose:
        print(f"Searching for contact: {name}")

    for attempt in range(max_retries):
        result = await meshcore.commands.get_contacts()

        if debug:
            print(
                f"DEBUG: Attempt {attempt + 1}, result type: {type(result)}, result is None: {result is None}",
                file=sys.stderr,
            )

        if result is None:
            if debug:
                print(
                    f"DEBUG: get_contacts() returned None on attempt {attempt + 1}",
                    file=sys.stderr,
                )
            if attempt < max_retries - 1:
                if debug:
                    print(
                        f"Failed to get contacts, retrying ({attempt + 1}/{max_retries})...",
                        file=sys.stderr,
                    )
                elif verbose:
                    print(
                        f"Failed to get contacts, retrying ({attempt + 1}/{max_retries})..."
                    )
                # No sleep - retry immediately
                continue
            else:
                if debug:
                    print(
                        "ERROR: Failed to get contacts after all retries (timeout or connection issue)",
                        file=sys.stderr,
                    )
                elif verbose:
                    print(
                        "ERROR: Failed to get contacts after all retries (timeout or connection issue)"
                    )
                return None

        if result.type == EventType.ERROR:
            if debug:
                print(f"DEBUG: ERROR event: {result.payload}", file=sys.stderr)
            if attempt < max_retries - 1:
                if verbose or debug:
                    print(f"Error getting contacts: {result.payload}, retrying...")
                await asyncio.sleep(0.5)
                continue
            else:
                if verbose:
                    print(f"ERROR: Failed to get contacts: {result.payload}")
                return None

        contacts = result.payload

        if debug:
            print(
                f"DEBUG: contacts type: {type(contacts)}, len: {len(contacts) if contacts else 0}",
                file=sys.stderr,
            )
            if contacts:
                print(
                    f"DEBUG: contact names: {[c.get('adv_name', 'NO_NAME') for c in contacts.values()]}",
                    file=sys.stderr,
                )

        if not contacts:
            # Empty contact list - retry instead of giving up
            if attempt < max_retries - 1:
                if debug:
                    print(
                        f"No contacts found, retrying ({attempt + 1}/{max_retries})...",
                        file=sys.stderr,
                    )
                elif verbose:
                    print(
                        f"No contacts found, retrying ({attempt + 1}/{max_retries})..."
                    )
                await asyncio.sleep(0.5)
                continue
            else:
                if verbose:
                    print("No contacts found on the device after all retries.")
                return None

        name_lower = name.lower()

        for contact_id, contact in contacts.items():
            contact_name = contact.get("adv_name", "")
            if contact_name and name_lower in contact_name.lower():
                if verbose:
                    print(f"✓ Found matching contact: {contact_name}")
                return {"id": contact_id, "data": contact, "name": contact_name}

        # Contact not in list - retry instead of giving up
        if attempt < max_retries - 1:
            if debug:
                print(
                    f"Contact '{name}' not in list, retrying ({attempt + 1}/{max_retries})...",
                    file=sys.stderr,
                )
            elif verbose:
                print(
                    f"Contact '{name}' not in list, retrying ({attempt + 1}/{max_retries})..."
                )
            await asyncio.sleep(0.5)
            continue

        if verbose:
            print(f"ERROR: No contact found matching '{name}'")
            print("\nAvailable contacts:")
            for contact_id, contact in contacts.items():
                contact_name = contact.get("adv_name", "Unknown")
                print(f"  - {contact_name}")
        return None

    return None


async def find_contact_by_public_key(
    meshcore, public_key: str, verbose=True, max_retries=5, debug=False
):
    """Find contact by public key (exact prefix match, case-insensitive) with retry logic."""
    if verbose:
        print(f"Searching for contact by public key: {public_key}")

    public_key_lower = public_key.lower()

    for attempt in range(max_retries):
        result = await meshcore.commands.get_contacts()

        if result is None:
            if attempt < max_retries - 1:
                await asyncio.sleep(0.5)
                continue
            if verbose:
                print("ERROR: Failed to get contacts after all retries")
            return None

        if result.type == EventType.ERROR:
            if attempt < max_retries - 1:
                await asyncio.sleep(0.5)
                continue
            if verbose:
                print(f"ERROR: Failed to get contacts: {result.payload}")
            return None

        contacts = result.payload
        if not contacts:
            if attempt < max_retries - 1:
                await asyncio.sleep(0.5)
                continue
            if verbose:
                print("No contacts found on the device after all retries.")
            return None

        for contact_id, contact in contacts.items():
            contact_pk = contact.get("public_key", "") or ""
            if (
                contact_pk.lower().startswith(public_key_lower)
                or contact_pk.lower() == public_key_lower
            ):
                contact_name = contact.get("adv_name", "")
                if verbose:
                    print(f"✓ Found contact by public key: {contact_name}")
                return {"id": contact_id, "data": contact, "name": contact_name}

        if attempt < max_retries - 1:
            await asyncio.sleep(0.5)
            continue

        if verbose:
            print(f"ERROR: No contact found with public key '{public_key}'")
        return None

    return None


async def get_status(meshcore, contact, password, verbose=True, max_retries=3):
    """Request and retrieve status from a contact with retry logic"""

    await login_to_contact(meshcore, contact, password, verbose)

    if verbose:
        print(f"\nRequesting status from {contact['name']}...")
        print("(This may take up to 15 seconds...)\n")

    contact_data = contact["data"]
    if verbose and contact_data.get("public_key"):
        print(f"Contact Public Key: {contact_data['public_key']}")

    # Get pubkey_prefix for filtering the response (use 12 chars like meshcore-hass)
    pubkey_prefix = (
        contact_data.get("public_key", "")[:12]
        if contact_data.get("public_key")
        else None
    )
    if verbose and pubkey_prefix:
        print(f"Using pubkey prefix for filtering: {pubkey_prefix}")

    for attempt in range(max_retries):
        try:
            if attempt > 0:
                wait_time = 2  # Fixed 2 second wait between retries
                if verbose:
                    print(
                        f"Retry attempt {attempt + 1}/{max_retries} (waiting {wait_time}s)..."
                    )
                await asyncio.sleep(wait_time)

            # Use the same pattern as meshcore-hass: send_binary_req + wait_for_event
            # Send the status request
            await meshcore.commands.send_binary_req(contact_data, BinaryReqType.STATUS)

            if verbose:
                print("Status request sent, waiting for response...")

            # Wait for the response with proper filtering (using MeshCore.wait_for_event directly)
            timeout = 15  # 15 second timeout for status response
            if pubkey_prefix:
                result = await meshcore.wait_for_event(
                    EventType.STATUS_RESPONSE,
                    attribute_filters={"pubkey_prefix": pubkey_prefix},
                    timeout=timeout,
                )
            else:
                result = await meshcore.wait_for_event(
                    EventType.STATUS_RESPONSE, timeout=timeout
                )

            if result is None:
                if attempt < max_retries - 1:
                    if verbose:
                        print("No response received, retrying...")
                    continue
                else:
                    if verbose:
                        print("ERROR: No status response received after all retries")
                        print("Possible reasons:")
                        print("  - Contact is offline or out of range")
                        print("  - Contact does not support status requests")
                        print("  - Radio interference or weak signal")
                    return None

            # Extract status data from the event payload
            status_data = result.payload if hasattr(result, "payload") else result
            return status_data

        except asyncio.TimeoutError:
            if attempt < max_retries - 1:
                if verbose:
                    print("Request timed out, retrying...")
                continue
            else:
                if verbose:
                    print("ERROR: Status request timed out after all retries")
                return None
        except Exception as e:
            if attempt < max_retries - 1:
                if verbose:
                    print(f"Request failed: {e}, retrying...")
                continue
            else:
                if verbose:
                    print(f"ERROR: Failed to get status after all retries: {e}")
                return None

    return None


def lpp_to_sensors(lpp_data: list) -> dict:
    """
    Convert a parsed LPP (Cayenne Low Power Payload) list into a flat sensor dict.

    The meshcore library returns TELEMETRY_RESPONSE payloads as a list of dicts::

        [{"channel": 1, "type": {"...": ...}, "value": 23.5}, ...]

    LPP type names used here match those in ``meshcore/lpp_json_encoder.py``:
      - ``"temperature"``  → °C
      - ``"humidity"``     → %
      - ``"barometer"``    → hPa

    Only the first occurrence of each sensor type is used (in case a device
    reports multiple channels for the same quantity).
    """
    sensors: dict = {}

    if not lpp_data:
        return sensors

    for entry in lpp_data:
        # The "type" field may be a string (type name) or a dict; normalise to str.
        type_name: str = ""
        raw_type = entry.get("type", "")
        if isinstance(raw_type, str):
            type_name = raw_type
        elif isinstance(raw_type, dict):
            # Some versions encode as {"name": "temperature", ...}
            type_name = raw_type.get("name", str(raw_type))

        value = entry.get("value")

        if type_name == "temperature" and "temperature_c" not in sensors:
            try:
                sensors["temperature_c"] = round(float(value), 2)
            except (TypeError, ValueError):
                logger.debug("Could not parse temperature value: %r", value)

        elif type_name == "humidity" and "humidity_pct" not in sensors:
            try:
                sensors["humidity_pct"] = round(float(value), 2)
            except (TypeError, ValueError):
                logger.debug("Could not parse humidity value: %r", value)

        elif type_name == "barometer" and "pressure_hpa" not in sensors:
            try:
                sensors["pressure_hpa"] = round(float(value), 2)
            except (TypeError, ValueError):
                logger.debug("Could not parse pressure value: %r", value)

    return sensors


async def get_sensor_telemetry(
    meshcore, contact, verbose: bool = True, max_retries: int = 3
) -> dict | None:
    """
    Request sensor telemetry (temperature, humidity, pressure) from a contact.

    Uses ``BinaryReqType.TELEMETRY`` → waits for ``EventType.TELEMETRY_RESPONSE``.
    The response payload contains an ``"lpp"`` key with a list of LPP-encoded
    sensor readings.

    Returns a dict with zero or more of:
      ``temperature_c``, ``humidity_pct``, ``pressure_hpa``

    Returns ``None`` if the request times out or the device does not support it,
    and an empty dict ``{}`` if the device responded but reported no sensor data.
    """
    contact_data = contact["data"]
    pubkey_prefix = (
        contact_data.get("public_key", "")[:12]
        if contact_data.get("public_key")
        else None
    )

    for attempt in range(max_retries):
        try:
            if attempt > 0:
                if verbose:
                    print(f"Sensor telemetry retry {attempt + 1}/{max_retries}...")
                await asyncio.sleep(2)

            await meshcore.commands.send_binary_req(
                contact_data, BinaryReqType.TELEMETRY
            )

            if verbose:
                print("Sensor telemetry request sent, waiting for response...")

            timeout = 20

            # Try filtering by pubkey_prefix if available; fall back to unfiltered.
            if pubkey_prefix:
                result = await meshcore.wait_for_event(
                    EventType.TELEMETRY_RESPONSE,
                    attribute_filters={"pubkey_prefix": pubkey_prefix},
                    timeout=timeout,
                )
            else:
                result = await meshcore.wait_for_event(
                    EventType.TELEMETRY_RESPONSE, timeout=timeout
                )

            if result is None:
                logger.debug(
                    "No TELEMETRY_RESPONSE from %s (attempt %d/%d) — "
                    "device may not support sensor telemetry",
                    contact["name"],
                    attempt + 1,
                    max_retries,
                )
                if attempt < max_retries - 1:
                    continue
                return None

            payload = result.payload if hasattr(result, "payload") else result
            logger.debug(
                "Raw TELEMETRY_RESPONSE payload from %s: %r", contact["name"], payload
            )

            lpp_data = None
            if isinstance(payload, dict):
                lpp_data = payload.get("lpp")
            elif isinstance(payload, list):
                lpp_data = payload

            if lpp_data is None:
                logger.debug(
                    "TELEMETRY_RESPONSE from %s had no 'lpp' key; payload keys: %s",
                    contact["name"],
                    list(payload.keys())
                    if isinstance(payload, dict)
                    else type(payload),
                )
                return {}

            sensors = lpp_to_sensors(lpp_data)
            if verbose:
                if sensors:
                    print(f"Sensor readings: {sensors}")
                else:
                    print(
                        "No sensor readings in telemetry response (device may lack sensors)"
                    )
            return sensors

        except asyncio.TimeoutError:
            logger.debug(
                "Sensor telemetry timeout for %s (attempt %d/%d)",
                contact["name"],
                attempt + 1,
                max_retries,
            )
            if attempt < max_retries - 1:
                continue
            return None
        except Exception as exc:
            logger.warning(
                "Sensor telemetry error for %s (attempt %d/%d): %s",
                contact["name"],
                attempt + 1,
                max_retries,
                exc,
            )
            if attempt < max_retries - 1:
                continue
            return None

    return None


def calculate_battery_percentage(bat_mv):
    """Calculate battery percentage from mV (0% at 3.2V, 100% at 4.2V)"""
    if bat_mv <= 0:
        return 0
    return max(0, min(100, (bat_mv - 3200) / (4200 - 3200) * 100))


def status_to_dict(status_data, contact_name=None, public_key=None):
    """Convert status data to dictionary"""
    bat_mv = status_data.get("bat", 0)
    bat_v = bat_mv / 1000
    bat_pct = calculate_battery_percentage(bat_mv)

    uptime = status_data.get("uptime", 0)
    uptime_days = uptime // 86400
    uptime_hours = (uptime % 86400) // 3600
    uptime_mins = (uptime % 3600) // 60
    uptime_secs = uptime % 60

    result = {
        "contact_name": contact_name,
        "battery": {
            "mv": bat_mv,
            "v": round(bat_v, 3),
            "percentage": round(bat_pct, 1),
        },
        "uptime": {
            "seconds": uptime,
            "days": uptime_days,
            "hours": uptime_hours,
            "minutes": uptime_mins,
            "seconds_rem": uptime_secs,
        },
        "radio": {
            "noise_floor": status_data.get("noise_floor", 0),
            "last_rssi": status_data.get("last_rssi", 0),
            "last_snr": round(status_data.get("last_snr", 0), 2),
            "tx_queue": status_data.get("tx_queue_len", 0),
            "queue_full_events": status_data.get("full_evts", 0),
        },
        "packets": {
            "sent": {
                "total": status_data.get("nb_sent", 0),
                "flood": status_data.get("sent_flood", 0),
                "direct": status_data.get("sent_direct", 0),
            },
            "received": {
                "total": status_data.get("nb_recv", 0),
                "flood": status_data.get("recv_flood", 0),
                "direct": status_data.get("recv_direct", 0),
            },
            "duplicates": {
                "direct": status_data.get("direct_dups", 0),
                "flood": status_data.get("flood_dups", 0),
            },
        },
        "airtime": {
            "tx": status_data.get("airtime", 0),
            "rx": status_data.get("rx_airtime", 0),
        },
        "public_key": public_key,
        "pubkey_prefix": status_data.get("pubkey_pre", None),
    }

    return result
