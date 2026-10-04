"""Real socket: server sends a `command` message right after identify
(a PENDING command was already queued for the device before it connected),
client replies `ack`, and the DeviceCommand must reach COMPLETED/FAILED
accordingly. See test_server_lifecycle.py's module docstring for why these
use `django_db(transaction=True)`.
"""

import asyncio
import json
import time

import pytest
from asgiref.sync import sync_to_async

from apps.tracking.models import DeviceCommand
from apps.tracking.telematics_service import server as server_module
from apps.tracking.tests.factories import DEFAULT_TEST_SECRET, DeviceCommandFactory, TrackingDeviceFactory

pytestmark = pytest.mark.django_db(transaction=True)


async def _wait_for_status(command, statuses, timeout=5):
    """Polls for an eventual state transition instead of a fixed sleep.

    handle_ack() performs two sequential cross-thread DB round-trips
    (run_sync calls) before a command reaches a terminal status; under
    full-suite thread-pool contention those can take longer than a fixed
    sleep, so we poll with a generous timeout instead of guessing a delay.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await sync_to_async(command.refresh_from_db, thread_sensitive=False)()
        if command.status in statuses:
            return
        await asyncio.sleep(0.05)


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


class TestCommandAckRoundTrip:
    def test_ack_ok_completes_the_command(self):
        device = TrackingDeviceFactory()
        command = DeviceCommandFactory(device=device, status=DeviceCommand.Status.PENDING)

        async def scenario(state):
            reader, writer = await asyncio.open_connection("127.0.0.1", state["port"])
            await _send_line(writer, {"type": "identify", "imei": device.imei, "secret": DEFAULT_TEST_SECRET})
            await asyncio.wait_for(reader.readline(), timeout=5)  # identify_ack

            # The server's post-identify dispatch should have claimed+sent
            # the pre-existing PENDING command immediately.
            command_line = await asyncio.wait_for(reader.readline(), timeout=5)
            sent_message = json.loads(command_line)
            assert sent_message["type"] == "command"
            assert sent_message["id"] == str(command.uuid)

            await _send_line(writer, {
                "type": "ack", "command_id": sent_message["id"], "status": "ok",
                "response_payload": {"battery": "80%"},
            })
            await _wait_for_status(command, {DeviceCommand.Status.COMPLETED, DeviceCommand.Status.FAILED})
            writer.close()
            await writer.wait_closed()

        asyncio.run(_run_scenario(scenario))

        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.COMPLETED
        assert command.response_payload == {"battery": "80%"}

    def test_ack_error_fails_the_command(self):
        device = TrackingDeviceFactory()
        command = DeviceCommandFactory(device=device, status=DeviceCommand.Status.PENDING)

        async def scenario(state):
            reader, writer = await asyncio.open_connection("127.0.0.1", state["port"])
            await _send_line(writer, {"type": "identify", "imei": device.imei, "secret": DEFAULT_TEST_SECRET})
            await asyncio.wait_for(reader.readline(), timeout=5)
            command_line = await asyncio.wait_for(reader.readline(), timeout=5)
            sent_message = json.loads(command_line)

            await _send_line(writer, {"type": "ack", "command_id": sent_message["id"], "status": "error"})
            await _wait_for_status(command, {DeviceCommand.Status.COMPLETED, DeviceCommand.Status.FAILED})
            writer.close()
            await writer.wait_closed()

        asyncio.run(_run_scenario(scenario))
        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.FAILED

    def test_stale_ack_for_already_terminal_command_is_a_noop(self):
        device = TrackingDeviceFactory()
        command = DeviceCommandFactory(device=device, status=DeviceCommand.Status.COMPLETED)

        async def scenario(state):
            reader, writer = await asyncio.open_connection("127.0.0.1", state["port"])
            await _send_line(writer, {"type": "identify", "imei": device.imei, "secret": DEFAULT_TEST_SECRET})
            await asyncio.wait_for(reader.readline(), timeout=5)  # identify_ack only — nothing PENDING to dispatch

            await _send_line(writer, {"type": "ack", "command_id": str(command.uuid), "status": "ok"})
            await asyncio.sleep(0.5)
            writer.close()
            await writer.wait_closed()

        asyncio.run(_run_scenario(scenario))
        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.COMPLETED  # unchanged, not re-processed

    def test_ack_for_unknown_command_id_is_ignored_without_crashing(self):
        device = TrackingDeviceFactory()

        async def scenario(state):
            reader, writer = await asyncio.open_connection("127.0.0.1", state["port"])
            await _send_line(writer, {"type": "identify", "imei": device.imei, "secret": DEFAULT_TEST_SECRET})
            await asyncio.wait_for(reader.readline(), timeout=5)

            await _send_line(writer, {"type": "ack", "command_id": "00000000-0000-0000-0000-000000000000", "status": "ok"})
            await asyncio.sleep(0.3)
            # Connection must still be alive/functional afterward.
            await _send_line(writer, {"type": "heartbeat"})
            await asyncio.sleep(0.2)
            writer.close()
            await writer.wait_closed()

        asyncio.run(_run_scenario(scenario))  # no exception == handled gracefully
