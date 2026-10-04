"""Generic JSON provider — the one fully-implemented parser this phase ships.

Accepts either a single event object or a batch ``{"events": [...]}``:

    {"timestamp": "2026-08-21T10:30:00Z", "latitude": 12.9716,
     "longitude": 77.5946, "speed": 45.2, "heading": 180, "ignition": true}

Every other provider (Teltonika/MDVR/NavTelecom) is a reserved
``TrackingDevice.Provider`` code with no parser yet — add a sibling module
implementing ``BaseProviderParser`` and register it in
``apps.tracking.providers.PROVIDER_PARSERS`` when one is actually needed.
"""

from decimal import Decimal, InvalidOperation

from django.utils.dateparse import parse_datetime

from apps.tracking.providers.base import BaseProviderParser, NormalizedEvent

_KNOWN_FIELDS = {
    "timestamp", "latitude", "longitude", "speed", "heading", "altitude", "ignition",
    "odometer", "engine_hours", "battery_voltage", "external_power", "signal_strength", "satellite_count",
}


def _to_decimal(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"could not parse numeric value {value!r}")


def _to_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"could not parse integer value {value!r}")


def _to_bool(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.lower() in ("true", "1", "yes", "on"):
            return True
        if value.lower() in ("false", "0", "no", "off"):
            return False
    raise ValueError(f"could not parse boolean value {value!r}")


class GenericJSONParser(BaseProviderParser):
    def parse(self, raw_payload):
        if not isinstance(raw_payload, dict):
            return [], [{"index": 0, "error": "payload must be a JSON object"}]

        items = raw_payload["events"] if "events" in raw_payload else [raw_payload]
        if not isinstance(items, list):
            return [], [{"index": 0, "error": "'events' must be a list"}]

        events, errors = [], []
        for index, item in enumerate(items):
            try:
                events.append(self._parse_item(item))
            except (ValueError, TypeError, AttributeError) as exc:
                errors.append({"index": index, "error": str(exc)})
        return events, errors

    def _parse_item(self, item):
        if not isinstance(item, dict):
            raise ValueError("event must be a JSON object")

        raw_timestamp = item.get("timestamp")
        if not raw_timestamp:
            raise ValueError("missing 'timestamp'")
        timestamp = parse_datetime(str(raw_timestamp))
        if timestamp is None:
            raise ValueError(f"could not parse timestamp {raw_timestamp!r}")

        if "latitude" not in item or item["latitude"] in (None, ""):
            raise ValueError("missing 'latitude'")
        if "longitude" not in item or item["longitude"] in (None, ""):
            raise ValueError("missing 'longitude'")
        latitude = _to_decimal(item["latitude"])
        longitude = _to_decimal(item["longitude"])

        metadata = {k: v for k, v in item.items() if k not in _KNOWN_FIELDS}

        return NormalizedEvent(
            timestamp=timestamp,
            latitude=latitude,
            longitude=longitude,
            speed=_to_decimal(item.get("speed")),
            heading=_to_int(item.get("heading")),
            altitude=_to_decimal(item.get("altitude")),
            ignition=_to_bool(item.get("ignition")),
            odometer=_to_decimal(item.get("odometer")),
            engine_hours=_to_decimal(item.get("engine_hours")),
            battery_voltage=_to_decimal(item.get("battery_voltage")),
            external_power=_to_bool(item.get("external_power")),
            signal_strength=_to_int(item.get("signal_strength")),
            satellite_count=_to_int(item.get("satellite_count")),
            metadata=metadata,
        )
