import struct

import pytest

from apps.tracking.telematics_service.protocol.base import FrameError
from apps.tracking.telematics_service.protocol.teltonika import TeltonikaFramer, crc16_ibm
from apps.tracking.telematics_service.tests.teltonika_fixtures import (
    DEFAULT_IMEI,
    build_avl_packet,
    build_avl_record_bytes,
    build_codec12_response_packet,
    build_heartbeat_packet,
    build_login_packet,
    build_unsupported_codec_packet,
)


class TestCrc16Ibm:
    def test_known_vector_empty(self):
        assert crc16_ibm(b"") == 0x0000

    def test_known_vector_ascii(self):
        # CRC-16/ARC ("123456789") is a widely published test vector for
        # this exact algorithm (poly 0xA001, init 0x0000, no xorout) = 0xBB3D.
        assert crc16_ibm(b"123456789") == 0xBB3D

    def test_deterministic(self):
        data = b"\x08\x01" + build_avl_record_bytes() + b"\x01"
        assert crc16_ibm(data) == crc16_ibm(data)

    def test_differs_for_different_data(self):
        assert crc16_ibm(b"abc") != crc16_ibm(b"abd")


def _logged_in_framer():
    framer = TeltonikaFramer()
    framer.feed(build_login_packet())
    return framer


class TestLoginPacket:
    def test_valid_login_emits_identify(self):
        framer = TeltonikaFramer()
        messages = framer.feed(build_login_packet("860123456789012"))
        assert messages == [{"type": "identify", "imei": "860123456789012"}]

    def test_login_split_across_two_feeds(self):
        framer = TeltonikaFramer()
        packet = build_login_packet()
        assert framer.feed(packet[:5]) == []
        assert framer.feed(packet[5:]) == [{"type": "identify", "imei": DEFAULT_IMEI}]

    def test_zero_length_imei_rejected(self):
        framer = TeltonikaFramer()
        with pytest.raises(FrameError):
            framer.feed(struct.pack(">H", 0))

    def test_non_digit_imei_rejected(self):
        framer = TeltonikaFramer()
        bad_imei = b"NOTANUMBER12345"
        with pytest.raises(FrameError):
            framer.feed(struct.pack(">H", len(bad_imei)) + bad_imei)


class TestAvlPacketDecoding:
    def test_single_record(self):
        framer = _logged_in_framer()
        record = build_avl_record_bytes(latitude=12.9716, longitude=77.5946, speed=45, io1={239: 1})
        messages = framer.feed(build_avl_packet([record]))
        assert len(messages) == 1
        assert messages[0]["type"] == "telemetry"
        decoded = messages[0]["records"][0]
        assert decoded["latitude_raw"] == 129716000
        assert decoded["longitude_raw"] == 775946000
        assert decoded["speed"] == 45
        assert decoded["io"] == {239: 1}

    def test_multiple_records_in_one_packet(self):
        framer = _logged_in_framer()
        records = [
            build_avl_record_bytes(timestamp_ms=1_700_000_000_000),
            build_avl_record_bytes(timestamp_ms=1_700_000_060_000),
            build_avl_record_bytes(timestamp_ms=1_700_000_120_000),
        ]
        messages = framer.feed(build_avl_packet(records))
        assert len(messages[0]["records"]) == 3

    def test_io_groups_of_every_size(self):
        framer = _logged_in_framer()
        record = build_avl_record_bytes(io1={1: 5}, io2={66: 12000}, io4={16: 999999}, io8={100: 123456789012})
        messages = framer.feed(build_avl_packet([record]))
        io = messages[0]["records"][0]["io"]
        assert io == {1: 5, 66: 12000, 16: 999999, 100: 123456789012}

    def test_partial_packet_across_multiple_reads(self):
        framer = _logged_in_framer()
        packet = build_avl_packet([build_avl_record_bytes()])
        assert framer.feed(packet[:10]) == []
        assert framer.feed(packet[10:20]) == []
        messages = framer.feed(packet[20:])
        assert messages[0]["type"] == "telemetry"

    def test_multiple_packets_in_one_read(self):
        framer = _logged_in_framer()
        packet1 = build_avl_packet([build_avl_record_bytes(timestamp_ms=1_700_000_000_000)])
        packet2 = build_avl_packet([build_avl_record_bytes(timestamp_ms=1_700_000_060_000)])
        messages = framer.feed(packet1 + packet2)
        assert len(messages) == 2
        assert messages[0]["records"][0]["timestamp_ms"] == 1_700_000_000_000
        assert messages[1]["records"][0]["timestamp_ms"] == 1_700_000_060_000

    def test_zero_records_is_heartbeat(self):
        framer = _logged_in_framer()
        messages = framer.feed(build_heartbeat_packet())
        assert messages == [{"type": "heartbeat"}]

    def test_invalid_declared_length_raises(self):
        framer = _logged_in_framer()
        bad_packet = b"\x00\x00\x00\x00" + struct.pack(">I", 999_999_999)  # absurd length, way over the frame cap
        with pytest.raises(FrameError):
            framer.feed(bad_packet)

    def test_oversized_frame_raises(self):
        framer = TeltonikaFramer(max_frame_bytes=50)
        framer.feed(build_login_packet())
        record = build_avl_record_bytes(io8={100: 1, 101: 2, 102: 3, 103: 4})  # padded to exceed 50 bytes
        with pytest.raises(FrameError):
            framer.feed(build_avl_packet([record, record, record]))

    def test_invalid_crc_is_rejected_not_raised(self):
        """A checksum failure is a rejected packet, not a fatal framing
        error — the connection must stay usable for the next packet."""
        framer = _logged_in_framer()
        bad_packet = build_avl_packet([build_avl_record_bytes()], bad_crc=True)
        messages = framer.feed(bad_packet)
        assert messages[0]["type"] == "_malformed"
        assert messages[0]["reason"] == "crc_mismatch"

    def test_record_count_mismatch_raises(self):
        framer = _logged_in_framer()
        bad_packet = build_avl_packet([build_avl_record_bytes()], bad_record_count_2=True)
        with pytest.raises(FrameError):
            framer.feed(bad_packet)

    def test_invalid_preamble_raises(self):
        framer = _logged_in_framer()
        packet = build_avl_packet([build_avl_record_bytes()])
        corrupted = b"\x00\x00\x00\x01" + packet[4:]
        with pytest.raises(FrameError):
            framer.feed(corrupted)

    def test_truncated_binary_does_not_crash_just_waits_for_more(self):
        framer = _logged_in_framer()
        packet = build_avl_packet([build_avl_record_bytes()])
        # Feed everything except the last 2 bytes — framer must simply wait,
        # not raise, since the declared length hasn't arrived yet.
        assert framer.feed(packet[:-2]) == []

    def test_malformed_record_content_raises_cleanly(self):
        """A packet whose declared record count implies more bytes than
        actually exist inside the data field (corrupted mid-record) must
        raise FrameError, never an unhandled struct/index exception."""
        framer = _logged_in_framer()
        good = build_avl_packet([build_avl_record_bytes()])
        # Claim 2 records but only provide bytes for 1 — corrupt the count
        # byte (index 9, right after preamble+length+codec_id) from 1 to 2,
        # then leave the CRC as-is so the corruption is caught during
        # record parsing, not silently CRC-rejected first.
        corrupted = bytearray(good)
        # Recompute so CRC still matches the (now-invalid) declared count.
        from apps.tracking.telematics_service.protocol.teltonika import crc16_ibm as _crc

        data_field = bytearray(corrupted[8:-4])
        data_field[1] = 2  # claim 2 records instead of 1
        new_crc = _crc(bytes(data_field))
        new_packet = bytes(corrupted[:4]) + struct.pack(">I", len(data_field)) + bytes(data_field) + struct.pack(">I", new_crc)
        with pytest.raises(FrameError):
            framer.feed(new_packet)


class TestUnsupportedCodec:
    def test_codec8_extended_rejected_cleanly(self):
        framer = _logged_in_framer()
        with pytest.raises(FrameError):
            framer.feed(build_unsupported_codec_packet(0x8E))

    def test_unknown_codec_rejected_cleanly(self):
        framer = _logged_in_framer()
        with pytest.raises(FrameError):
            framer.feed(build_unsupported_codec_packet(0xFF))


class TestCodec12Response:
    def test_response_decoded(self):
        framer = _logged_in_framer()
        messages = framer.feed(build_codec12_response_packet("getinfo OK"))
        assert messages == [{"type": "ack", "response_text": "getinfo OK"}]

    def test_response_split_across_reads(self):
        framer = _logged_in_framer()
        packet = build_codec12_response_packet("hello")
        assert framer.feed(packet[:6]) == []
        messages = framer.feed(packet[6:])
        assert messages[0]["response_text"] == "hello"
