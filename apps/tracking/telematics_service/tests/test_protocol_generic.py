import json

import pytest

from apps.tracking.telematics_service.protocol.base import FrameError
from apps.tracking.telematics_service.protocol.generic import NDJSONFramer


class TestNDJSONFramer:
    def test_single_complete_message(self):
        framer = NDJSONFramer()
        messages = framer.feed(json.dumps({"type": "heartbeat"}).encode() + b"\n")
        assert messages == [{"type": "heartbeat"}]

    def test_partial_message_across_two_feeds(self):
        framer = NDJSONFramer()
        payload = json.dumps({"type": "heartbeat"}).encode() + b"\n"
        first_half, second_half = payload[:5], payload[5:]
        assert framer.feed(first_half) == []
        assert framer.feed(second_half) == [{"type": "heartbeat"}]

    def test_multiple_messages_in_one_feed(self):
        framer = NDJSONFramer()
        data = (
            json.dumps({"type": "heartbeat"}).encode() + b"\n" +
            json.dumps({"type": "ping"}).encode() + b"\n"
        )
        messages = framer.feed(data)
        assert messages == [{"type": "heartbeat"}, {"type": "ping"}]

    def test_empty_feed_returns_nothing(self):
        framer = NDJSONFramer()
        assert framer.feed(b"") == []

    def test_blank_lines_are_skipped(self):
        framer = NDJSONFramer()
        messages = framer.feed(b"\n\n" + json.dumps({"type": "heartbeat"}).encode() + b"\n")
        assert messages == [{"type": "heartbeat"}]

    def test_malformed_json_yields_marker_not_exception(self):
        framer = NDJSONFramer()
        messages = framer.feed(b"not-json-at-all\n")
        assert len(messages) == 1
        assert messages[0]["type"] == "_malformed"

    def test_non_object_json_yields_marker(self):
        framer = NDJSONFramer()
        messages = framer.feed(b"[1, 2, 3]\n")
        assert messages[0]["type"] == "_malformed"

    def test_oversized_frame_raises(self):
        framer = NDJSONFramer(max_frame_bytes=10)
        with pytest.raises(FrameError):
            framer.feed(b"x" * 100)  # no newline — never completes a frame

    def test_frame_under_limit_with_terminator_does_not_raise(self):
        framer = NDJSONFramer(max_frame_bytes=10)
        messages = framer.feed(b'{"a":1}\n')
        assert messages == [{"a": 1}]

    def test_connection_never_crashes_on_one_bad_line(self):
        """A malformed line followed by a valid one — the valid one must
        still be extracted."""
        framer = NDJSONFramer()
        data = b"garbage\n" + json.dumps({"type": "heartbeat"}).encode() + b"\n"
        messages = framer.feed(data)
        assert messages[0]["type"] == "_malformed"
        assert messages[1] == {"type": "heartbeat"}
