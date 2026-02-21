"""
Unit tests for telemetry_common helpers — no device or ClickHouse required.
"""

import pytest
from app.meshcore.telemetry_common import lpp_to_sensors, calculate_battery_percentage


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
