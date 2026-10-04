"""Teltonika Codec 8 provider — turns the decoded AVL record dicts produced
by ``apps.tracking.telematics_service.protocol.teltonika.TeltonikaFramer``
into ``NormalizedEvent``s, via the exact same ``BaseProviderParser``
interface every other provider (including ``GenericJSONParser``) uses.

Unit conversion and Teltonika "AVL ID" -> domain-field mapping live here,
not in the framer, which only decodes bytes into raw protocol integers.

AVL ID mapping used (Teltonika's standard, widely-published IO ID registry,
shared across their FM/FMB device family for these basic parameters):
    239  Ignition          (0/1 boolean)
    16   Total Odometer    (metres -> converted to km, matching this app's
                             existing odometer convention elsewhere)
    67   Battery Voltage   (millivolts -> converted to volts)
    21   GSM Signal        (0-5 quality scale, NOT dBm — stored as-is in
                             signal_strength; documented so it is never
                             confused with a dBm reading another provider
                             might supply)
    66   External Voltage  (millivolts) — a raw voltage reading, not a
                             boolean, so it is preserved in metadata only;
                             external_power is left None rather than
                             inventing a threshold the base spec doesn't
                             define.
Any other IO ID present on a record is preserved verbatim in ``metadata``
(e.g. ``{"io_1": 0, "io_240": 1}``) so nothing decoded is silently dropped.
"""

from datetime import datetime, timezone
from decimal import Decimal

from apps.tracking.providers.base import BaseProviderParser, NormalizedEvent

IGNITION_IO_ID = 239
TOTAL_ODOMETER_IO_ID = 16
BATTERY_VOLTAGE_IO_ID = 67
GSM_SIGNAL_IO_ID = 21
EXTERNAL_VOLTAGE_IO_ID = 66

_MAPPED_IO_IDS = {IGNITION_IO_ID, TOTAL_ODOMETER_IO_ID, BATTERY_VOLTAGE_IO_ID, GSM_SIGNAL_IO_ID}


class TeltonikaAVLParser(BaseProviderParser):
    def parse(self, raw_payload: dict) -> tuple[list[NormalizedEvent], list[dict]]:
        records = raw_payload.get("records")
        if not isinstance(records, list):
            return [], [{"index": 0, "error": "missing or invalid 'records' list"}]

        events = []
        errors = []
        for index, record in enumerate(records):
            try:
                events.append(self._parse_record(record))
            except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                errors.append({"index": index, "error": f"could not parse AVL record: {exc}"})
        return events, errors

    @staticmethod
    def _parse_record(record: dict) -> NormalizedEvent:
        timestamp = datetime.fromtimestamp(record["timestamp_ms"] / 1000, tz=timezone.utc)
        longitude = Decimal(record["longitude_raw"]) / Decimal(10_000_000)
        latitude = Decimal(record["latitude_raw"]) / Decimal(10_000_000)
        io = record.get("io") or {}

        ignition = None
        if IGNITION_IO_ID in io:
            ignition = bool(io[IGNITION_IO_ID])

        odometer = None
        if TOTAL_ODOMETER_IO_ID in io:
            odometer = Decimal(io[TOTAL_ODOMETER_IO_ID]) / Decimal(1000)  # metres -> km

        battery_voltage = None
        if BATTERY_VOLTAGE_IO_ID in io:
            battery_voltage = Decimal(io[BATTERY_VOLTAGE_IO_ID]) / Decimal(1000)  # mV -> V

        signal_strength = io.get(GSM_SIGNAL_IO_ID)  # 0-5 quality scale, not dBm

        metadata = {f"io_{io_id}": value for io_id, value in io.items() if io_id not in _MAPPED_IO_IDS}
        if record.get("event_io_id"):
            metadata["event_io_id"] = record["event_io_id"]
        if record.get("priority") is not None:
            metadata["priority"] = record["priority"]

        return NormalizedEvent(
            timestamp=timestamp,
            latitude=latitude,
            longitude=longitude,
            speed=Decimal(record["speed"]) if record.get("speed") is not None else None,
            heading=record.get("heading"),
            altitude=Decimal(record["altitude"]) if record.get("altitude") is not None else None,
            ignition=ignition,
            odometer=odometer,
            battery_voltage=battery_voltage,
            external_power=None,  # base protocol gives a voltage reading, not a flag — see module docstring
            signal_strength=signal_strength,
            satellite_count=record.get("satellites"),
            metadata=metadata,
        )
