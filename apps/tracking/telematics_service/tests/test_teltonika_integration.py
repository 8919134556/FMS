"""Real socket, real Teltonika Codec 8 bytes: proves the full pipeline —
communication service -> TeltonikaFramer -> TeltonikaAVLParser ->
TelemetryIngestionService -> RawTelemetryEvent/TelemetryEvent/
VehicleCurrentTelemetry — end to end, with no synthetic JSON shortcuts.
See test_server_lifecycle.py's module docstring for why these use
`django_db(transaction=True)`.
"""

import asyncio

import pytest

from apps.tracking.models import RawTelemetryEvent, TelemetryEvent, TrackingDevice, VehicleCurrentTelemetry
from apps.tracking.telematics_service import server as server_module
from apps.tracking.telematics_service.tests.teltonika_fixtures import (
    build_avl_packet,
    build_avl_record_bytes,
    build_login_packet,
)
from apps.tracking.tests.factories import TrackingDeviceFactory
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db(transaction=True)


async def _run_scenario(scenario):
    stop_event = asyncio.Event()
    ready = asyncio.Event()
    state = {}

    def on_ready(servers, connection_manager):
        state["connection_manager"] = connection_manager
        state["teltonika_port"] = servers["teltonika"].sockets[0].getsockname()[1]
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


class TestTeltonikaTelemetryIntegration:
    def test_login_and_avl_packet_creates_telemetry(self):
        vehicle = VehicleFactory()
        device = TrackingDeviceFactory(vehicle=vehicle, provider=TrackingDevice.Provider.TELTONIKA)

        async def scenario(state):
            reader, writer = await asyncio.open_connection("127.0.0.1", state["teltonika_port"])
            writer.write(build_login_packet(device.imei))
            await writer.drain()
            login_ack = await asyncio.wait_for(reader.read(1), timeout=5)
            assert login_ack == b"\x01"

            record = build_avl_record_bytes(latitude=12.9716, longitude=77.5946, speed=45)
            writer.write(build_avl_packet([record]))
            await writer.drain()
            telemetry_ack = await asyncio.wait_for(reader.readexactly(4), timeout=5)
            assert int.from_bytes(telemetry_ack, "big") == 1  # 1 record accepted

            writer.close()
            await writer.wait_closed()

        asyncio.run(_run_scenario(scenario))

        assert RawTelemetryEvent.objects.filter(device=device).exists()
        assert TelemetryEvent.objects.filter(device=device).exists()
        current = VehicleCurrentTelemetry.objects.get(vehicle=vehicle)
        assert str(current.latitude) == "12.971600"

    def test_unknown_imei_rejected_with_reject_byte(self):
        async def scenario(state):
            reader, writer = await asyncio.open_connection("127.0.0.1", state["teltonika_port"])
            writer.write(build_login_packet("000000000000000"))
            await writer.drain()
            login_ack = await asyncio.wait_for(reader.read(1), timeout=5)
            assert login_ack == b"\x00"
            writer.close()
            await writer.wait_closed()

        asyncio.run(_run_scenario(scenario))

    def test_batch_of_records_all_accepted(self):
        vehicle = VehicleFactory()
        device = TrackingDeviceFactory(vehicle=vehicle, provider=TrackingDevice.Provider.TELTONIKA)

        async def scenario(state):
            reader, writer = await asyncio.open_connection("127.0.0.1", state["teltonika_port"])
            writer.write(build_login_packet(device.imei))
            await writer.drain()
            await asyncio.wait_for(reader.read(1), timeout=5)

            records = [
                build_avl_record_bytes(timestamp_ms=1_700_000_000_000),
                build_avl_record_bytes(timestamp_ms=1_700_000_060_000),
                build_avl_record_bytes(timestamp_ms=1_700_000_120_000),
            ]
            writer.write(build_avl_packet(records))
            await writer.drain()
            ack = await asyncio.wait_for(reader.readexactly(4), timeout=5)
            assert int.from_bytes(ack, "big") == 3

            writer.close()
            await writer.wait_closed()

        asyncio.run(_run_scenario(scenario))
        assert TelemetryEvent.objects.filter(device=device).count() == 3

    def test_duplicate_packet_deduped(self):
        vehicle = VehicleFactory()
        device = TrackingDeviceFactory(vehicle=vehicle, provider=TrackingDevice.Provider.TELTONIKA)
        record = build_avl_record_bytes(timestamp_ms=1_700_000_000_000)

        async def scenario(state):
            reader, writer = await asyncio.open_connection("127.0.0.1", state["teltonika_port"])
            writer.write(build_login_packet(device.imei))
            await writer.drain()
            await asyncio.wait_for(reader.read(1), timeout=5)

            writer.write(build_avl_packet([record]))
            await writer.drain()
            await asyncio.wait_for(reader.readexactly(4), timeout=5)

            writer.write(build_avl_packet([record]))  # exact same record again
            await writer.drain()
            await asyncio.wait_for(reader.readexactly(4), timeout=5)

            writer.close()
            await writer.wait_closed()

        asyncio.run(_run_scenario(scenario))
        assert TelemetryEvent.objects.filter(device=device).count() == 1

    def test_out_of_order_packet_does_not_regress_current_position(self):
        vehicle = VehicleFactory()
        device = TrackingDeviceFactory(vehicle=vehicle, provider=TrackingDevice.Provider.TELTONIKA)

        async def scenario(state):
            reader, writer = await asyncio.open_connection("127.0.0.1", state["teltonika_port"])
            writer.write(build_login_packet(device.imei))
            await writer.drain()
            await asyncio.wait_for(reader.read(1), timeout=5)

            newer = build_avl_record_bytes(timestamp_ms=1_700_000_100_000, latitude=12.9716, longitude=77.5946)
            writer.write(build_avl_packet([newer]))
            await writer.drain()
            await asyncio.wait_for(reader.readexactly(4), timeout=5)

            older = build_avl_record_bytes(timestamp_ms=1_700_000_000_000, latitude=1.0, longitude=1.0)
            writer.write(build_avl_packet([older]))
            await writer.drain()
            await asyncio.wait_for(reader.readexactly(4), timeout=5)

            writer.close()
            await writer.wait_closed()

        asyncio.run(_run_scenario(scenario))
        current = VehicleCurrentTelemetry.objects.get(vehicle=vehicle)
        assert str(current.latitude) == "12.971600"  # unchanged by the older packet

    def test_crc_mismatch_packet_gets_no_ack_but_connection_stays_open(self):
        device = TrackingDeviceFactory(provider=TrackingDevice.Provider.TELTONIKA)

        async def scenario(state):
            reader, writer = await asyncio.open_connection("127.0.0.1", state["teltonika_port"])
            writer.write(build_login_packet(device.imei))
            await writer.drain()
            await asyncio.wait_for(reader.read(1), timeout=5)

            bad_packet = build_avl_packet([build_avl_record_bytes()], bad_crc=True)
            writer.write(bad_packet)
            await writer.drain()

            # No ACK for the bad packet — but the connection is still alive:
            # a subsequent good packet must still be processed normally.
            good_record = build_avl_record_bytes()
            writer.write(build_avl_packet([good_record]))
            await writer.drain()
            ack = await asyncio.wait_for(reader.readexactly(4), timeout=5)
            assert int.from_bytes(ack, "big") == 1

            writer.close()
            await writer.wait_closed()

        asyncio.run(_run_scenario(scenario))
        assert TelemetryEvent.objects.filter(device=device).count() == 1

    def test_reconnect_after_disconnect(self):
        device = TrackingDeviceFactory(provider=TrackingDevice.Provider.TELTONIKA)

        async def scenario(state):
            reader1, writer1 = await asyncio.open_connection("127.0.0.1", state["teltonika_port"])
            writer1.write(build_login_packet(device.imei))
            await writer1.drain()
            assert await asyncio.wait_for(reader1.read(1), timeout=5) == b"\x01"
            writer1.close()
            await writer1.wait_closed()
            await asyncio.sleep(0.5)

            reader2, writer2 = await asyncio.open_connection("127.0.0.1", state["teltonika_port"])
            writer2.write(build_login_packet(device.imei))
            await writer2.drain()
            assert await asyncio.wait_for(reader2.read(1), timeout=5) == b"\x01"
            writer2.close()
            await writer2.wait_closed()

        asyncio.run(_run_scenario(scenario))
