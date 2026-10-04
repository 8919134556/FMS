"""Newline-delimited JSON (NDJSON) framing — the Generic Socket Protocol
this phase ships as a testable test/dev connection format.

Message envelope (one JSON object per line): ``{"type": "identify"|
"telemetry"|"heartbeat"|"ack", ...}``. This is explicitly NOT a real
Teltonika/MDVR/NavTelecom wire protocol — those are binary, vendor-specific,
and not implemented here (see apps/tracking/providers/__init__.py's
UnsupportedProviderError for the equivalent decision on the HTTP ingestion
side). A real vendor adapter would add its own ``BaseFramer`` subclass
(e.g. length-prefixed binary frames) alongside this one.
"""

import json

from apps.tracking.telematics_service import config
from apps.tracking.telematics_service.protocol.base import BaseFramer, FrameError


class NDJSONFramer(BaseFramer):
    def __init__(self, max_frame_bytes=None):
        self._buffer = bytearray()
        self._max_frame_bytes = config.MAX_FRAME_BYTES if max_frame_bytes is None else max_frame_bytes

    def feed(self, data: bytes) -> list[dict]:
        self._buffer.extend(data)
        messages = []
        while b"\n" in self._buffer:
            line, _, rest = self._buffer.partition(b"\n")
            self._buffer = bytearray(rest)
            line = line.strip()
            if line:
                messages.append(self._decode(line))
        if len(self._buffer) > self._max_frame_bytes:
            size = len(self._buffer)
            self._buffer = bytearray()  # drop it — the connection is being closed by the caller anyway
            raise FrameError(f"Frame of {size} bytes exceeds the {self._max_frame_bytes}-byte limit without a terminator.")
        return messages

    @staticmethod
    def _decode(line: bytes) -> dict:
        try:
            decoded = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"type": "_malformed", "raw_length": len(line)}
        if not isinstance(decoded, dict):
            return {"type": "_malformed", "raw_length": len(line)}
        return decoded
