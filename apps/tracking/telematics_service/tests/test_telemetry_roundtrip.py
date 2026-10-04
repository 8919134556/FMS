"""Proves the TCP path reuses apps.tracking.services.TelemetryIngestionService
verbatim — no parallel/competing ingestion pipeline. See
test_server_lifecycle.py's module docstring for why these use
`django_db(transaction=True)`.
"""

import asyncio
import json

import pytest

from apps.tracking.models import RawTelemetryEvent, TelemetryEvent, VehicleCurrentTelemetry
from apps.tracking.telematics_service import server as server_module
from apps.tracking.tests.factories import DEFAULT_TEST_SECRET, TrackingDeviceFactory
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db(transaction=True)


async def _run_scenario(scenario):
    stop_event = asyncio.Event()
    ready = asyncio.Event()
    state = {}

    def on_ready(servers, connection_manager):
        state["connection_manager"] = connection_manager
        state["port"] = servers["generic"].sockets[0].getsockname()[1]
        ready.set()

    server_task = asyncio.create_task(
        server_module.run_server(
            stop_event=stop_event, host="127.0.0.1", generic_port=0, teltonika_port=0, on_ready=on_ready
        )
    )
    await asyncio.wait_for(ready.wait(), timeout=5)
    try:
        await scenario(state)
    finally:
        stop_event.set()
        await asyncio.wait_for(server_task, timeout=5)
    return state


async def _send_line(writer, message: dict):
    writer.write((json.dumps(message) + "\n").encode())
    await writer.drain()


class TestTelemetryReachesExistingIngestionService:
    def test_single_telemetry_message_creates_raw_and_normalized_events(self):
        vehicle = VehicleFactory()
        device = TrackingDeviceFactory(vehicle=vehicle)

        async def scenario(state):
            reader, writer = await asyncio.open_connection("127.0.0.1", state["port"])
            await _send_line(writer, {"type": "identify", "imei": device.imei, "secret": DEFAULT_TEST_SECRET})
            await asyncio.wait_for(reader.readline(), timeout=5)

            await _send_line(writer, {
                "type": "telemetry", "timestamp": "2026-08-22T10:30:00Z",
                "latitude": 12.9716, "longitude": 77.5946, "speed": 45.2,
            })
            await asyncio.sleep(0.5)  # let the server process the message
            writer.close()
            await writer.wait_closed()

        asyncio.run(_run_scenario(scenario))

        assert RawTelemetryEvent.objects.filter(device=device).exists()
        assert TelemetryEvent.objects.filter(device=device).exists()
        current = VehicleCurrentTelemetry.objects.get(vehicle=vehicle)
        assert str(current.latitude) == "12.971600"

    def test_batch_telemetry_message(self):
        vehicle = VehicleFactory()
        device = TrackingDeviceFactory(vehicle=vehicle)

        async def scenario(state):
            reader, writer = await asyncio.open_connection("127.0.0.1", state["port"])
            await _send_line(writer, {"type": "identify", "imei": device.imei, "secret": DEFAULT_TEST_SECRET})
            await asyncio.wait_for(reader.readline(), timeout=5)

            await _send_line(writer, {
                "type": "telemetry",
                "events": [
                    {"timestamp": "2026-08-22T10:00:00Z", "latitude": 12.0, "longitude": 77.0},
                    {"timestamp": "2026-08-22T10:05:00Z", "latitude": 12.1, "longitude": 77.1},
                ],
            })
            await asyncio.sleep(0.5)
            writer.close()
            await writer.wait_closed()

        asyncio.run(_run_scenario(scenario))
        assert TelemetryEvent.objects.filter(device=device).count() == 2

    def test_duplicate_telemetry_deduped(self):
        """Same dedup constraint as the HTTP ingestion path — no separate
        rules for the TCP transport."""
        vehicle = VehicleFactory()
        device = TrackingDeviceFactory(vehicle=vehicle)
        message = {
            "type": "telemetry", "timestamp": "2026-08-22T10:30:00Z",
            "latitude": 12.9716, "longitude": 77.5946,
        }

        async def scenario(state):
            reader, writer = await asyncio.open_connection("127.0.0.1", state["port"])
            await _send_line(writer, {"type": "identify", "imei": device.imei, "secret": DEFAULT_TEST_SECRET})
            await asyncio.wait_for(reader.readline(), timeout=5)
            await _send_line(writer, message)
            await asyncio.sleep(0.3)
            await _send_line(writer, message)
            await asyncio.sleep(0.3)
            writer.close()
            await writer.wait_closed()

        asyncio.run(_run_scenario(scenario))
        assert TelemetryEvent.objects.filter(device=device).count() == 1

    def test_out_of_order_telemetry_does_not_regress_current_position(self):
        vehicle = VehicleFactory()
        device = TrackingDeviceFactory(vehicle=vehicle)

        async def scenario(state):
            reader, writer = await asyncio.open_connection("127.0.0.1", state["port"])
            await _send_line(writer, {"type": "identify", "imei": device.imei, "secret": DEFAULT_TEST_SECRET})
            await asyncio.wait_for(reader.readline(), timeout=5)

            await _send_line(writer, {
                "type": "telemetry", "timestamp": "2026-08-22T10:30:00Z",
                "latitude": 12.9716, "longitude": 77.5946,
            })
            await asyncio.sleep(0.3)
            await _send_line(writer, {
                "type": "telemetry", "timestamp": "2026-08-22T10:00:00Z",  # older
                "latitude": 1.0, "longitude": 1.0,
            })
            await asyncio.sleep(0.3)
            writer.close()
            await writer.wait_closed()

        asyncio.run(_run_scenario(scenario))
        current = VehicleCurrentTelemetry.objects.get(vehicle=vehicle)
        assert str(current.latitude) == "12.971600"  # unchanged by the older event

    def test_heartbeat_does_not_touch_telemetry_tables(self):
        vehicle = VehicleFactory()
        device = TrackingDeviceFactory(vehicle=vehicle)

        async def scenario(state):
            reader, writer = await asyncio.open_connection("127.0.0.1", state["port"])
            await _send_line(writer, {"type": "identify", "imei": device.imei, "secret": DEFAULT_TEST_SECRET})
            await asyncio.wait_for(reader.readline(), timeout=5)
            await _send_line(writer, {"type": "heartbeat"})
            await asyncio.sleep(0.3)
            writer.close()
            await writer.wait_closed()

        asyncio.run(_run_scenario(scenario))
        assert not TelemetryEvent.objects.filter(device=device).exists()
        assert not RawTelemetryEvent.objects.filter(device=device).exists()
        assert not VehicleCurrentTelemetry.objects.filter(vehicle=vehicle).exists()
        device.refresh_from_db()
        assert device.last_communication is None
