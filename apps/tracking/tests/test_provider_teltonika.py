import datetime
from decimal import Decimal

import pytest

from apps.tracking.providers.teltonika import TeltonikaAVLParser
from apps.tracking.services import validate_normalized_event

pytestmark = pytest.mark.django_db  # touches settings-driven validate_normalized_event


def _record(**overrides):
    base = {
        "timestamp_ms": 1_700_000_000_000,
        "priority": 1,
        "longitude_raw": 775946000,
        "latitude_raw": 129716000,
        "altitude": 920,
        "heading": 180,
        "satellites": 8,
        "speed": 45,
        "event_io_id": 0,
        "io": {},
    }
    base.update(overrides)
    return base


class TestTeltonikaAVLParser:
    def test_valid_record_produces_normalized_event(self):
        events, errors = TeltonikaAVLParser().parse({"records": [_record()]})
        assert errors == []
        assert len(events) == 1
        event = events[0]
        assert event.latitude == Decimal("12.9716")
        assert event.longitude == Decimal("77.5946")
        assert event.speed == Decimal(45)
        assert event.heading == 180
        assert event.altitude == Decimal(920)
        assert event.satellite_count == 8
        assert isinstance(event.timestamp, datetime.datetime)

    def test_ignition_mapped_from_io_239(self):
        events, _ = TeltonikaAVLParser().parse({"records": [_record(io={239: 1})]})
        assert events[0].ignition is True

    def test_ignition_off(self):
        events, _ = TeltonikaAVLParser().parse({"records": [_record(io={239: 0})]})
        assert events[0].ignition is False

    def test_ignition_none_when_io_absent(self):
        events, _ = TeltonikaAVLParser().parse({"records": [_record(io={})]})
        assert events[0].ignition is None

    def test_odometer_converted_metres_to_km(self):
        events, _ = TeltonikaAVLParser().parse({"records": [_record(io={16: 123456})]})
        assert events[0].odometer == Decimal("123.456")

    def test_odometer_none_when_io_absent(self):
        events, _ = TeltonikaAVLParser().parse({"records": [_record(io={})]})
        assert events[0].odometer is None

    def test_battery_voltage_converted_mv_to_v(self):
        events, _ = TeltonikaAVLParser().parse({"records": [_record(io={67: 12450})]})
        assert events[0].battery_voltage == Decimal("12.45")

    def test_signal_strength_from_io_21(self):
        events, _ = TeltonikaAVLParser().parse({"records": [_record(io={21: 4})]})
        assert events[0].signal_strength == 4

    def test_external_power_never_fabricated(self):
        events, _ = TeltonikaAVLParser().parse({"records": [_record(io={66: 13800})]})
        assert events[0].external_power is None  # base protocol gives a voltage, not a flag

    def test_unmapped_io_preserved_in_metadata(self):
        events, _ = TeltonikaAVLParser().parse({"records": [_record(io={1: 5, 240: 1})]})
        assert events[0].metadata["io_1"] == 5
        assert events[0].metadata["io_240"] == 1
        assert "io_239" not in events[0].metadata  # mapped IDs excluded from metadata

    def test_multiple_records_in_one_payload(self):
        events, errors = TeltonikaAVLParser().parse({
            "records": [
                _record(timestamp_ms=1_700_000_000_000),
                _record(timestamp_ms=1_700_000_060_000),
            ]
        })
        assert errors == []
        assert len(events) == 2

    def test_missing_records_key_yields_error_not_crash(self):
        events, errors = TeltonikaAVLParser().parse({})
        assert events == []
        assert len(errors) == 1

    def test_malformed_record_yields_per_item_error(self):
        events, errors = TeltonikaAVLParser().parse({"records": [{"garbage": True}]})
        assert events == []
        assert len(errors) == 1
        assert errors[0]["index"] == 0

    def test_one_bad_record_does_not_fail_the_whole_batch(self):
        events, errors = TeltonikaAVLParser().parse({"records": [_record(), {"garbage": True}]})
        assert len(events) == 1
        assert len(errors) == 1

    def test_valid_event_passes_existing_domain_validation(self):
        """Reuses apps.tracking.services.validate_normalized_event —
        proves coordinate/speed/timestamp validation is not re-implemented."""
        events, _ = TeltonikaAVLParser().parse({"records": [_record()]})
        assert validate_normalized_event(events[0]) == []

    def test_out_of_range_latitude_caught_by_existing_validation(self):
        events, _ = TeltonikaAVLParser().parse({"records": [_record(latitude_raw=950_000_000)]})  # 95 degrees
        problems = validate_normalized_event(events[0])
        assert any("latitude" in p for p in problems)
