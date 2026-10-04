"""Connection-level framing contract — how raw bytes become discrete
message dicts. Kept strictly separate from apps.tracking.providers (how a
decoded dict becomes NormalizedEvent telemetry) — a framer never knows
about telemetry semantics, and a provider parser never sees raw bytes.

    Socket bytes -> Frame extraction (this module) -> decoded dict
        -> apps.tracking.providers parser -> NormalizedEvent
        -> apps.tracking.services.TelemetryIngestionService
"""


class FrameError(Exception):
    """A connection-level protocol violation (oversized frame). The caller
    must treat this as fatal for the connection — log and close, never
    crash the accept loop."""


class BaseFramer:
    """Subclass and implement ``feed()`` for a new connection-level framing
    scheme (e.g. a vendor's length-prefixed binary frames)."""

    def feed(self, data: bytes) -> list[dict]:
        """Buffers ``data`` and returns zero or more complete, decoded
        message dicts. Raises ``FrameError`` only for a connection-fatal
        violation (e.g. buffer overflow before a frame boundary is found) —
        a single malformed/undecodable frame should be surfaced as a
        ``{"type": "_malformed", ...}`` entry, not an exception, so one bad
        line never kills an otherwise-healthy connection."""
        raise NotImplementedError
