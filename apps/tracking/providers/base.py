"""Provider parser contract.

A parser turns one provider's raw ingestion payload into a list of
``NormalizedEvent``s. Parsing (shape/format problems) is kept separate from
domain validation (out-of-range coordinates/speed) — the latter lives in
``apps.tracking.services.validate_normalized_event`` and runs uniformly
across every provider after parsing.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass
class NormalizedEvent:
    timestamp: datetime
    latitude: Decimal
    longitude: Decimal
    speed: Decimal | None = None
    heading: int | None = None
    altitude: Decimal | None = None
    ignition: bool | None = None
    odometer: Decimal | None = None
    engine_hours: Decimal | None = None
    battery_voltage: Decimal | None = None
    external_power: bool | None = None
    signal_strength: int | None = None
    satellite_count: int | None = None
    metadata: dict = field(default_factory=dict)


class BaseProviderParser:
    """Subclass and implement ``parse()`` for a new provider."""

    def parse(self, raw_payload: dict) -> tuple[list[NormalizedEvent], list[dict]]:
        """Returns ``(events, errors)``. ``errors`` is a list of
        ``{"index": int, "error": str}`` for individual malformed items —
        one bad point in a batch must not fail the whole batch."""
        raise NotImplementedError
