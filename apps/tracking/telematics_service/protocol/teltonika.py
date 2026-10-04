"""Teltonika Codec 8 (AVL data) + Codec 12 (GPRS command/response) binary
framing — built directly from Teltonika Telematics' publicly documented TCP/IP
AVL data protocol ("Codec" specification): 4-byte zero preamble, 4-byte
big-endian data-field length, 1-byte Codec ID, AVL records (GPS + IO
elements), CRC-16/IBM checksum. Login is a separate, simpler packet: a
2-byte length-prefixed ASCII IMEI, with NO secret/password field in the base
protocol (see ``apps.tracking.telematics_service.auth.
authenticate_teltonika_device_sync`` for how this is handled — documented
limitation, not an oversight).

This module only implements base **Codec 8** (codec ID ``0x08``) — Codec 8
Extended (``0x8E``, 2-byte IO IDs) is NOT implemented; a packet using it is
cleanly rejected as an unsupported codec, never mis-parsed as Codec 8.

Byte layout (all multi-byte integers big-endian):

Login packet::

    [0:2]   uint16  IMEI length
    [2:N]   ascii   IMEI

AVL data packet::

    [0:4]   bytes   zero preamble (0x00000000)
    [4:8]   uint32  data-field length
    [8:...] bytes   data field (CRC is computed over exactly this slice):
        [0]     uint8   codec ID (0x08 = Codec 8, 0x0C = Codec 12)
        [1]     uint8   number of records (Codec 8 only)
        ...     AVL record(s), see _parse_avl_record
        [-1]    uint8   number of records, repeated (must match)
    [...:+4] uint32  CRC-16/IBM of the data field

AVL record::

    [0:8]   uint64  timestamp, milliseconds since Unix epoch (UTC)
    [8]     uint8   priority
    [9:13]  int32   longitude * 10^7 (signed two's complement)
    [13:17] int32   latitude * 10^7 (signed two's complement)
    [17:19] int16   altitude, metres
    [19:21] uint16  heading, degrees
    [21]    uint8   satellite count
    [22:24] uint16  speed, km/h
    [24]    uint8   event IO ID (0 if this record wasn't IO-triggered)
    [25]    uint8   total IO element count (informational, N1+N2+N4+N8)
    [26]    uint8   N1 (1-byte IO values) then N1 * (1-byte ID + 1-byte value)
    ...     uint8   N2 (2-byte IO values) then N2 * (1-byte ID + 2-byte value)
    ...     uint8   N4 (4-byte IO values) then N4 * (1-byte ID + 4-byte value)
    ...     uint8   N8 (8-byte IO values) then N8 * (1-byte ID + 8-byte value)

Codec 12 (GPRS command channel) shares the same outer envelope; its data
field is::

    [0]     uint8   codec ID (0x0C)
    [1]     uint8   quantity 1
    [2]     uint8   type (0x05 = command, 0x06 = response)
    [3:7]   uint32  command/response text length
    [7:+N]  ascii   command/response text
    [-1]    uint8   quantity 2 (must match quantity 1)
"""

import struct

from apps.tracking.telematics_service import config
from apps.tracking.telematics_service.protocol.base import BaseFramer, FrameError

CODEC8 = 0x08
CODEC12 = 0x0C
CODEC12_TYPE_COMMAND = 0x05
CODEC12_TYPE_RESPONSE = 0x06

# Real IMEIs are 15 digits; allow modest slack rather than hardcoding 15
# exactly, since some emulators/test rigs use shorter identifiers.
MAX_LOGIN_IMEI_LENGTH = 32

# Cap how much of a raw packet we ever stash in RawTelemetryEvent.payload —
# "preserve useful raw data" does not mean unbounded binary blobs.
RAW_HEX_PREVIEW_BYTES = 2048


def crc16_ibm(data: bytes) -> int:
    """CRC-16/IBM (a.k.a. CRC-16/ARC) — Teltonika's documented checksum
    algorithm for the AVL data field: polynomial 0xA001 (the reflected form
    of 0x8005), initial value 0x0000, no output XOR."""
    crc = 0x0000
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


class TeltonikaFramer(BaseFramer):
    """Stateful: the connection's first bytes are always the login (IMEI)
    packet — a fundamentally different shape from every packet after it —
    so this framer tracks whether login has completed."""

    def __init__(self, max_frame_bytes=None):
        self._buffer = bytearray()
        self._logged_in = False
        self._max_frame_bytes = config.TELTONIKA_MAX_FRAME_BYTES if max_frame_bytes is None else max_frame_bytes

    def feed(self, data: bytes) -> list[dict]:
        self._buffer.extend(data)
        messages = []
        while True:
            message = self._try_parse_one()
            if message is None:
                break
            messages.append(message)
        return messages

    def _try_parse_one(self):
        if not self._logged_in:
            return self._try_parse_login()
        return self._try_parse_data_packet()

    def _try_parse_login(self):
        if len(self._buffer) < 2:
            return None
        imei_length = struct.unpack(">H", self._buffer[0:2])[0]
        if imei_length == 0 or imei_length > MAX_LOGIN_IMEI_LENGTH:
            raise FrameError(f"Invalid login IMEI length: {imei_length}")
        if len(self._buffer) < 2 + imei_length:
            return None

        imei_bytes = bytes(self._buffer[2 : 2 + imei_length])
        del self._buffer[: 2 + imei_length]

        try:
            imei = imei_bytes.decode("ascii")
        except UnicodeDecodeError as exc:
            raise FrameError("Login IMEI is not valid ASCII.") from exc
        if not imei.isdigit():
            raise FrameError("Login IMEI must be numeric.")

        self._logged_in = True
        return {"type": "identify", "imei": imei}

    def _try_parse_data_packet(self):
        if len(self._buffer) < 8:
            return None
        preamble = bytes(self._buffer[0:4])
        if preamble != b"\x00\x00\x00\x00":
            raise FrameError(f"Invalid preamble: {preamble.hex()}")

        data_length = struct.unpack(">I", self._buffer[4:8])[0]
        if data_length == 0 or data_length > self._max_frame_bytes:
            raise FrameError(f"Invalid or oversized data-field length: {data_length}")

        total_length = 8 + data_length + 4
        if len(self._buffer) < total_length:
            return None  # wait for more bytes — never assume one read() == one packet

        data_field = bytes(self._buffer[8 : 8 + data_length])
        received_crc = struct.unpack(">I", self._buffer[8 + data_length : total_length])[0] & 0xFFFF
        raw_packet = bytes(self._buffer[:total_length])
        del self._buffer[:total_length]

        if crc16_ibm(data_field) != received_crc:
            # Rejected, not fatal — no ACK is sent for this packet, but the
            # connection stays open for the device's next (or retried) one.
            return {"type": "_malformed", "reason": "crc_mismatch", "raw_hex": raw_packet.hex()[:RAW_HEX_PREVIEW_BYTES]}

        try:
            codec_id = data_field[0]
            if codec_id == CODEC8:
                return self._parse_codec8(data_field, raw_packet)
            if codec_id == CODEC12:
                return self._parse_codec12(data_field, raw_packet)
        except (struct.error, IndexError) as exc:
            raise FrameError(f"Malformed data field: {exc}") from exc
        raise FrameError(f"Unsupported codec ID: 0x{codec_id:02X}")

    def _parse_codec8(self, data_field: bytes, raw_packet: bytes) -> dict:
        offset = 1
        record_count = data_field[offset]
        offset += 1

        records = []
        for _ in range(record_count):
            record, offset = self._parse_avl_record(data_field, offset)
            records.append(record)

        record_count_2 = data_field[offset]
        offset += 1
        if record_count_2 != record_count:
            raise FrameError("AVL record count mismatch (Number of Data 1 != Number of Data 2).")

        if not records:
            # A zero-record AVL packet is Codec 8's closest thing to a
            # keepalive — there is no dedicated heartbeat message type in
            # the base protocol.
            return {"type": "heartbeat"}
        return {"type": "telemetry", "records": records, "raw_hex": raw_packet.hex()[:RAW_HEX_PREVIEW_BYTES]}

    @staticmethod
    def _parse_avl_record(data_field: bytes, offset: int):
        timestamp_ms = struct.unpack(">Q", data_field[offset : offset + 8])[0]
        offset += 8
        priority = data_field[offset]
        offset += 1

        longitude_raw, latitude_raw = struct.unpack(">ii", data_field[offset : offset + 8])
        offset += 8
        altitude = struct.unpack(">h", data_field[offset : offset + 2])[0]
        offset += 2
        heading = struct.unpack(">H", data_field[offset : offset + 2])[0]
        offset += 2
        satellites = data_field[offset]
        offset += 1
        speed = struct.unpack(">H", data_field[offset : offset + 2])[0]
        offset += 2

        event_io_id = data_field[offset]
        offset += 1
        offset += 1  # total IO count — informational only (== N1+N2+N4+N8), not needed to parse

        io_values = {}
        for size_bytes, fmt in ((1, ">B"), (2, ">H"), (4, ">I"), (8, ">Q")):
            count = data_field[offset]
            offset += 1
            for _ in range(count):
                io_id = data_field[offset]
                offset += 1
                value = struct.unpack(fmt, data_field[offset : offset + size_bytes])[0]
                offset += size_bytes
                io_values[io_id] = value

        record = {
            "timestamp_ms": timestamp_ms,
            "priority": priority,
            # Left as raw, unscaled protocol integers — unit conversion
            # (degrees, km, volts, ...) is the PROVIDER layer's job
            # (apps.tracking.providers.teltonika), not the framer's.
            "longitude_raw": longitude_raw,
            "latitude_raw": latitude_raw,
            "altitude": altitude,
            "heading": heading,
            "satellites": satellites,
            "speed": speed,
            "event_io_id": event_io_id,
            "io": io_values,
        }
        return record, offset

    @staticmethod
    def _parse_codec12(data_field: bytes, raw_packet: bytes) -> dict:
        offset = 1
        quantity_1 = data_field[offset]
        offset += 1
        packet_type = data_field[offset]
        offset += 1
        size = struct.unpack(">I", data_field[offset : offset + 4])[0]
        offset += 4
        text = data_field[offset : offset + size].decode("ascii", errors="replace")
        offset += size
        quantity_2 = data_field[offset]
        offset += 1
        if quantity_2 != quantity_1:
            raise FrameError("Codec 12 quantity mismatch (quantity 1 != quantity 2).")

        if packet_type == CODEC12_TYPE_RESPONSE:
            return {"type": "ack", "response_text": text}
        # A device legitimately only ever sends Type 0x06 (response) — the
        # server is the only side that sends Type 0x05 (command). Anything
        # else arriving from a device is treated as malformed, never
        # silently accepted as if it were a real response.
        return {"type": "_malformed", "reason": "unexpected_codec12_type", "raw_hex": raw_packet.hex()[:RAW_HEX_PREVIEW_BYTES]}


def encode_login_response(ok: bool) -> bytes:
    """The real Codec 8 login response is a single accept(0x01)/reject(0x00)
    byte — the caller closes the connection on reject."""
    return bytes([0x01 if ok else 0x00])


def encode_telemetry_ack(count: int) -> bytes:
    """The real Codec 8 telemetry ACK: a 4-byte big-endian count of
    successfully accepted records (0 if the whole packet was rejected)."""
    return struct.pack(">I", count)


def encode_command(text: str) -> bytes:
    """Builds a real Codec 12 (Type 0x05, command) packet carrying ``text``
    verbatim as the command payload (e.g. "getinfo", "getgps", "reboot")."""
    command_bytes = text.encode("ascii")
    data_field = (
        bytes([CODEC12, 0x01, CODEC12_TYPE_COMMAND])
        + struct.pack(">I", len(command_bytes))
        + command_bytes
        + bytes([0x01])
    )
    crc = crc16_ibm(data_field)
    return b"\x00\x00\x00\x00" + struct.pack(">I", len(data_field)) + data_field + struct.pack(">I", crc)
