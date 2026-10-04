"""Deterministic, hand-computed Teltonika Codec 8/12 binary packet builders
for tests — real byte layouts per the protocol specification (see
apps.tracking.telematics_service.protocol.teltonika's module docstring),
not synthetic JSON. CRC is computed with the same algorithm under test, so
these fixtures double as a correctness check on crc16_ibm itself (any
accidental algorithm drift breaks nearly every test using these builders).
"""

import struct

from apps.tracking.telematics_service.protocol.teltonika import crc16_ibm

DEFAULT_IMEI = "123456789012345"


def build_login_packet(imei: str = DEFAULT_IMEI) -> bytes:
    return struct.pack(">H", len(imei)) + imei.encode("ascii")


def build_avl_record_bytes(
    *,
    timestamp_ms: int = 1_700_000_000_000,
    priority: int = 1,
    latitude: float = 12.9716,
    longitude: float = 77.5946,
    altitude: int = 920,
    heading: int = 180,
    satellites: int = 8,
    speed: int = 45,
    event_io_id: int = 0,
    io1: dict | None = None,
    io2: dict | None = None,
    io4: dict | None = None,
    io8: dict | None = None,
) -> bytes:
    io1, io2, io4, io8 = io1 or {}, io2 or {}, io4 or {}, io8 or {}
    total_io_count = len(io1) + len(io2) + len(io4) + len(io8)

    out = struct.pack(">Q", timestamp_ms)
    out += bytes([priority])
    out += struct.pack(">ii", int(round(longitude * 10_000_000)), int(round(latitude * 10_000_000)))
    out += struct.pack(">h", altitude)
    out += struct.pack(">H", heading)
    out += bytes([satellites])
    out += struct.pack(">H", speed)
    out += bytes([event_io_id])
    out += bytes([total_io_count])

    out += bytes([len(io1)])
    for io_id, value in io1.items():
        out += bytes([io_id]) + struct.pack(">B", value)
    out += bytes([len(io2)])
    for io_id, value in io2.items():
        out += bytes([io_id]) + struct.pack(">H", value)
    out += bytes([len(io4)])
    for io_id, value in io4.items():
        out += bytes([io_id]) + struct.pack(">I", value)
    out += bytes([len(io8)])
    for io_id, value in io8.items():
        out += bytes([io_id]) + struct.pack(">Q", value)
    return out


def build_avl_packet(records: list[bytes], *, bad_crc: bool = False, bad_record_count_2: bool = False) -> bytes:
    """Wraps pre-built AVL record byte blobs (see build_avl_record_bytes)
    into a full Codec 8 packet: preamble + length + data field + CRC."""
    count = len(records)
    count_2 = count if not bad_record_count_2 else count + 1
    data_field = bytes([0x08, count]) + b"".join(records) + bytes([count_2 & 0xFF])
    crc = crc16_ibm(data_field)
    if bad_crc:
        crc ^= 0xFFFF
    return b"\x00\x00\x00\x00" + struct.pack(">I", len(data_field)) + data_field + struct.pack(">I", crc)


def build_heartbeat_packet() -> bytes:
    """A zero-record AVL packet — Codec 8's keepalive."""
    return build_avl_packet([])


def build_codec12_response_packet(text: str) -> bytes:
    data_field = bytes([0x0C, 0x01, 0x06]) + struct.pack(">I", len(text)) + text.encode("ascii") + bytes([0x01])
    crc = crc16_ibm(data_field)
    return b"\x00\x00\x00\x00" + struct.pack(">I", len(data_field)) + data_field + struct.pack(">I", crc)


def build_unsupported_codec_packet(codec_id: int = 0x8E) -> bytes:
    data_field = bytes([codec_id, 0x01])
    crc = crc16_ibm(data_field)
    return b"\x00\x00\x00\x00" + struct.pack(">I", len(data_field)) + data_field + struct.pack(">I", crc)
