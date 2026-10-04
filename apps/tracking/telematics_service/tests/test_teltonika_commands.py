import asyncio
import time

import pytest
from asgiref.sync import sync_to_async

from apps.tracking.models import DeviceCommand, TrackingDevice
from apps.tracking.telematics_service import command_dispatch, command_encoding, server as server_module
from apps.tracking.telematics_service.protocol import teltonika as teltonika_protocol
from apps.tracking.telematics_service.tests.teltonika_fixtures import build_codec12_response_packet, build_login_packet
from apps.tracking.tests.factories import DeviceCommandFactory, TrackingDeviceFactory


@pytest.mark.django_db
class TestCommandEncoding:
    @pytest.mark.parametrize("command_type,expected_text", [
        (DeviceCommand.CommandType.PING, "getinfo"),
        (DeviceCommand.CommandType.REQUEST_LOCATION, "getgps"),
        (DeviceCommand.CommandType.REBOOT, "reboot"),
    ])
    def test_supported_commands_encode_to_real_codec12_bytes(self, command_type, expected_text):
        command = DeviceCommandFactory(command_type=command_type)
        encoded = command_encoding.encode_command_for_provider("teltonika", command)
        assert encoded == teltonika_protocol.encode_command(expected_text)

    def test_set_reporting_interval_unsupported_for_teltonika(self):
        command = DeviceCommandFactory(command_type=DeviceCommand.CommandType.SET_REPORTING_INTERVAL)
        with pytest.raises(command_encoding.UnsupportedCommandError):
            command_encoding.encode_command_for_provider("teltonika", command)

    def test_custom_with_command_text_encodes(self):
        command = DeviceCommandFactory(command_type=DeviceCommand.CommandType.CUSTOM, payload={"command_text": "getver"})
        encoded = command_encoding.encode_command_for_provider("teltonika", command)
        assert encoded == teltonika_protocol.encode_command("getver")

    def test_custom_without_command_text_raises(self):
        command = DeviceCommandFactory(command_type=DeviceCommand.CommandType.CUSTOM, payload={})
        with pytest.raises(command_encoding.UnsupportedCommandError):
            command_encoding.encode_command_for_provider("teltonika", command)

    def test_generic_provider_still_supports_all_five(self):
        for command_type in DeviceCommand.CommandType.values:
            assert command_type in command_encoding.SUPPORTED_COMMAND_TYPES["generic"]

    def test_supported_command_types_registry_matches_encoder_behavior(self):
        assert command_encoding.SUPPORTED_COMMAND_TYPES["teltonika"] == {
            DeviceCommand.CommandType.PING, DeviceCommand.CommandType.REQUEST_LOCATION,
            DeviceCommand.CommandType.REBOOT, DeviceCommand.CommandType.CUSTOM,
        }

    def test_unregistered_provider_raises(self):
        command = DeviceCommandFactory()
        with pytest.raises(command_encoding.UnsupportedCommandError):
            command_encoding.encode_command_for_provider("navtelecom", command)


@pytest.mark.django_db(transaction=True)
class TestUnsupportedCommandDispatchFailsCleanly:
    def test_dispatch_claimed_fails_unsupported_command_never_sends(self):
        """An UnsupportedCommandError at dispatch time must FAIL the
        command outright — not silently drop it, not retry forever."""
        from apps.tracking.telematics_service.connection_manager import ConnectionManager
        from apps.tracking.telematics_service.device_session import DeviceSession

        device = TrackingDeviceFactory(provider=TrackingDevice.Provider.TELTONIKA)
        command = DeviceCommandFactory(
            device=device, command_type=DeviceCommand.CommandType.SET_REPORTING_INTERVAL,
            status=DeviceCommand.Status.QUEUED,
        )

        class _FakeWriter:
            def write(self, data):
                raise AssertionError("must never attempt to send an unsupported command")

            async def drain(self):
                pass

        session = DeviceSession(reader=None, writer=_FakeWriter(), device=device, provider="teltonika")
        connection_manager = ConnectionManager()
        connection_manager.register(session)

        asyncio.run(command_dispatch._dispatch_claimed([command], connection_manager))

        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.FAILED
        assert "not supported" in command.last_error.lower()


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


async def _wait_for_status(command, statuses, timeout=5):
    """Polls for an eventual state transition instead of a fixed sleep.

    handle_teltonika_response() performs two sequential cross-thread DB
    round-trips (run_sync calls) before a command reaches a terminal
    status; under full-suite thread-pool contention those can take longer
    than a fixed sleep, so we poll with a generous timeout instead of
    guessing a delay (same fix as test_command_dispatch_ack.py).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await sync_to_async(command.refresh_from_db, thread_sensitive=False)()
        if command.status in statuses:
            return
        await asyncio.sleep(0.05)


@pytest.mark.django_db(transaction=True)
class TestCodec12ResponseCorrelation:
    def test_response_completes_the_oldest_sent_command(self):
        device = TrackingDeviceFactory(provider=TrackingDevice.Provider.TELTONIKA)
        command = DeviceCommandFactory(device=device, command_type=DeviceCommand.CommandType.PING, status=DeviceCommand.Status.PENDING)

        async def scenario(state):
            reader, writer = await asyncio.open_connection("127.0.0.1", state["teltonika_port"])
            writer.write(build_login_packet(device.imei))
            await writer.drain()
            await asyncio.wait_for(reader.read(1), timeout=5)  # login ack

            # The server dispatches the pending PING command immediately post-login.
            command_bytes = await asyncio.wait_for(reader.read(4096), timeout=5)
            assert command_bytes  # a real Codec 12 command packet arrived

            writer.write(build_codec12_response_packet("getinfo: battery 80%"))
            await writer.drain()
            await _wait_for_status(command, {DeviceCommand.Status.COMPLETED, DeviceCommand.Status.FAILED})
            writer.close()
            await writer.wait_closed()

        asyncio.run(_run_scenario(scenario))
        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.COMPLETED
        assert command.response_payload == {"response_text": "getinfo: battery 80%"}

    def test_response_with_no_outstanding_sent_command_is_a_noop(self):
        device = TrackingDeviceFactory(provider=TrackingDevice.Provider.TELTONIKA)

        async def scenario(state):
            reader, writer = await asyncio.open_connection("127.0.0.1", state["teltonika_port"])
            writer.write(build_login_packet(device.imei))
            await writer.drain()
            await asyncio.wait_for(reader.read(1), timeout=5)

            writer.write(build_codec12_response_packet("unexpected"))
            await writer.drain()
            await asyncio.sleep(0.3)

            # Connection must still be usable afterward.
            writer.write(build_codec12_response_packet("still fine"))
            await writer.drain()
            await asyncio.sleep(0.2)
            writer.close()
            await writer.wait_closed()

        asyncio.run(_run_scenario(scenario))  # no exception == handled gracefully

    def test_second_command_not_claimed_while_one_is_in_flight(self):
        device = TrackingDeviceFactory(provider=TrackingDevice.Provider.TELTONIKA)
        DeviceCommandFactory(device=device, status=DeviceCommand.Status.SENT)
        second = DeviceCommandFactory(device=device, status=DeviceCommand.Status.PENDING)

        claimed = command_dispatch._claim_and_queue_sync([device.pk])
        assert claimed == []
        second.refresh_from_db()
        assert second.status == DeviceCommand.Status.PENDING
