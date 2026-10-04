"""Per-connection state for one identified device."""

import json

from django.utils import timezone


class DeviceSession:
    def __init__(self, *, reader, writer, device, provider=None, peername=None):
        self.reader = reader
        self.writer = writer
        self.device = device
        # Which wire protocol this connection speaks — the value the device
        # was identified under, e.g. "generic" or "teltonika". Lets
        # command_dispatch pick the right command encoder without a second
        # DB lookup (see apps.tracking.telematics_service.command_encoding).
        self.provider = provider or device.provider
        self.peername = peername
        self.last_seen_at = timezone.now()

    def touch(self):
        self.last_seen_at = timezone.now()

    async def send_bytes(self, data: bytes):
        """The actual write+drain primitive. A raised exception here means
        the bytes did NOT reach the OS send buffer — callers (see
        command_dispatch._dispatch_claimed) treat that as "not sent",
        never as "maybe sent"."""
        self.writer.write(data)
        await self.writer.drain()

    async def send(self, message: dict):
        """NDJSON convenience wrapper over send_bytes — used by the generic
        protocol's identify_ack/etc. Unchanged behavior from Phase 3.5."""
        await self.send_bytes(json.dumps(message).encode("utf-8") + b"\n")
