"""
Unit tests for telemetry_common helpers — no device or ClickHouse required.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from meshcore import EventType

from app.meshcore import telemetry_common
from app.meshcore.telemetry_common import (
    _parse_path_hash_mode,
    calculate_battery_percentage,
    connect_to_device,
    load_config,
    lpp_to_sensors,
)


# ── lpp_to_sensors ────────────────────────────────────────────────────────────


def test_lpp_to_sensors_all_three():
    """Extracts temperature, humidity, and pressure from a typical LPP list."""
    lpp = [
        {"channel": 1, "type": "temperature", "value": 23.5},
        {"channel": 2, "type": "humidity", "value": 62.0},
        {"channel": 3, "type": "barometer", "value": 1012.5},
    ]
    sensors = lpp_to_sensors(lpp)
    assert sensors == {
        "temperature_c": 23.5,
        "humidity_pct": 62.0,
        "pressure_hpa": 1012.5,
    }


def test_lpp_to_sensors_temperature_only():
    lpp = [{"channel": 1, "type": "temperature", "value": 18.0}]
    sensors = lpp_to_sensors(lpp)
    assert sensors == {"temperature_c": 18.0}
    assert "humidity_pct" not in sensors
    assert "pressure_hpa" not in sensors


def test_lpp_to_sensors_empty_list():
    assert lpp_to_sensors([]) == {}


def test_lpp_to_sensors_none():
    assert lpp_to_sensors(None) == {}


def test_lpp_to_sensors_unknown_types_ignored():
    lpp = [
        {
            "channel": 1,
            "type": "accelerometer",
            "value": {"acc_x": 0, "acc_y": 0, "acc_z": 1},
        },
        {"channel": 2, "type": "temperature", "value": 20.0},
    ]
    sensors = lpp_to_sensors(lpp)
    assert sensors == {"temperature_c": 20.0}


def test_lpp_to_sensors_first_occurrence_wins():
    """If the device reports the same quantity on multiple channels, first wins."""
    lpp = [
        {"channel": 1, "type": "temperature", "value": 21.0},
        {"channel": 2, "type": "temperature", "value": 99.0},
    ]
    sensors = lpp_to_sensors(lpp)
    assert sensors["temperature_c"] == 21.0


def test_lpp_to_sensors_rounding():
    lpp = [{"channel": 1, "type": "temperature", "value": 23.456789}]
    sensors = lpp_to_sensors(lpp)
    assert sensors["temperature_c"] == 23.46


def test_lpp_to_sensors_type_as_dict():
    """type field may be a dict with a 'name' key."""
    lpp = [{"channel": 1, "type": {"name": "temperature"}, "value": 19.5}]
    sensors = lpp_to_sensors(lpp)
    assert sensors == {"temperature_c": 19.5}


# ── calculate_battery_percentage ──────────────────────────────────────────────


def test_battery_100_pct():
    assert calculate_battery_percentage(4200) == 100.0


def test_battery_0_pct():
    assert calculate_battery_percentage(3200) == 0.0


def test_battery_50_pct():
    assert calculate_battery_percentage(3700) == 50.0


def test_battery_clamped_at_0():
    assert calculate_battery_percentage(0) == 0
    assert calculate_battery_percentage(3100) == 0


def test_battery_clamped_at_100():
    assert calculate_battery_percentage(4500) == 100.0


# ── _parse_path_hash_mode ─────────────────────────────────────────────────────


def test_parse_path_hash_mode_valid():
    assert _parse_path_hash_mode("0") == 0
    assert _parse_path_hash_mode("1") == 1


def test_parse_path_hash_mode_invalid_falls_back_to_1():
    assert _parse_path_hash_mode("bogus") == 1
    assert _parse_path_hash_mode("5") == 1
    assert _parse_path_hash_mode("-1") == 1
    assert _parse_path_hash_mode("") == 1


# ── load_config ───────────────────────────────────────────────────────────────


def test_load_config_path_hash_mode_defaults_to_1(monkeypatch):
    """Default is 2-byte mode (1) when PATH_HASH_MODE is not set."""
    monkeypatch.delenv("PATH_HASH_MODE", raising=False)
    with patch("app.meshcore.telemetry_common.load_dotenv", lambda: None):
        config = load_config()
    assert config["path_hash_mode"] == 1


def test_load_config_path_hash_mode_env_override(monkeypatch):
    monkeypatch.setenv("PATH_HASH_MODE", "0")
    with patch("app.meshcore.telemetry_common.load_dotenv", lambda: None):
        config = load_config()
    assert config["path_hash_mode"] == 0


# ── connect_to_device path hash mode ──────────────────────────────────────────

_TCP_CONFIG = {
    "connection_type": "tcp",
    "tcp_host": "192.0.2.1",
    "tcp_port": 4000,
    "debug": False,
    "path_hash_mode": 1,
}


def _ok_event() -> MagicMock:
    event = MagicMock()
    event.type = EventType.OK
    return event


def _error_event() -> MagicMock:
    event = MagicMock()
    event.type = EventType.ERROR
    event.payload = "unsupported command"
    return event


@pytest.mark.asyncio
class TestConnectToDevicePathHashMode:
    async def test_sets_path_hash_mode_on_connect(self):
        """2-byte mode (1) from config is pushed to the device on connect."""
        mock_meshcore = AsyncMock()
        mock_meshcore.commands.set_path_hash_mode = AsyncMock(return_value=_ok_event())

        with patch.object(
            telemetry_common.MeshCore,
            "create_tcp",
            new=AsyncMock(return_value=mock_meshcore),
        ):
            result = await connect_to_device(_TCP_CONFIG, verbose=False)

        assert result is mock_meshcore
        mock_meshcore.commands.set_path_hash_mode.assert_awaited_once_with(1)

    async def test_sets_path_hash_mode_zero_when_configured(self):
        """1-byte mode (0) is honoured when explicitly configured."""
        mock_meshcore = AsyncMock()
        mock_meshcore.commands.set_path_hash_mode = AsyncMock(return_value=_ok_event())
        config = {**_TCP_CONFIG, "path_hash_mode": 0}

        with patch.object(
            telemetry_common.MeshCore,
            "create_tcp",
            new=AsyncMock(return_value=mock_meshcore),
        ):
            await connect_to_device(config, verbose=False)

        mock_meshcore.commands.set_path_hash_mode.assert_awaited_once_with(0)

    async def test_missing_config_key_defaults_to_mode_1(self):
        """Configs built before PATH_HASH_MODE existed still get 2-byte mode."""
        mock_meshcore = AsyncMock()
        mock_meshcore.commands.set_path_hash_mode = AsyncMock(return_value=_ok_event())
        config = {k: v for k, v in _TCP_CONFIG.items() if k != "path_hash_mode"}

        with patch.object(
            telemetry_common.MeshCore,
            "create_tcp",
            new=AsyncMock(return_value=mock_meshcore),
        ):
            await connect_to_device(config, verbose=False)

        mock_meshcore.commands.set_path_hash_mode.assert_awaited_once_with(1)

    async def test_error_response_does_not_fail_connection(self):
        """Firmware rejecting the command (e.g. < v10) keeps the connection."""
        mock_meshcore = AsyncMock()
        mock_meshcore.commands.set_path_hash_mode = AsyncMock(
            return_value=_error_event()
        )

        with patch.object(
            telemetry_common.MeshCore,
            "create_tcp",
            new=AsyncMock(return_value=mock_meshcore),
        ):
            result = await connect_to_device(_TCP_CONFIG, verbose=False)

        assert result is mock_meshcore

    async def test_exception_does_not_fail_connection(self):
        """A failing set_path_hash_mode call never raises out of connect."""
        mock_meshcore = AsyncMock()
        mock_meshcore.commands.set_path_hash_mode = AsyncMock(
            side_effect=RuntimeError("device gone")
        )

        with patch.object(
            telemetry_common.MeshCore,
            "create_tcp",
            new=AsyncMock(return_value=mock_meshcore),
        ):
            result = await connect_to_device(_TCP_CONFIG, verbose=False)

        assert result is mock_meshcore
