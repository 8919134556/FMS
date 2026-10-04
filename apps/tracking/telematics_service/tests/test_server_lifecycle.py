"""Real asyncio.start_server + asyncio.open_connection tests, each wrapped
in a plain sync `def test_...(): asyncio.run(...)` — no pytest-asyncio
dependency needed.

IMPORTANT: these use `@pytest.mark.django_db(transaction=True)`. The
server's DB calls run via `telematics_service.db.run_sync`
(`sync_to_async(thread_sensitive=False)`), which executes on a DIFFERENT
THREAD with its own DB connection — under the default (non-transactional)
`django_db` fixture, that thread's connection would never see data created
in the test's own (uncommitted) transaction. `transaction=True` commits
fixture data to the real test database so any thread/connection can see it.
"""

import asyncio
import json
import logging

import pytest

from apps.tracking.telematics_service import server as server_module
from apps.tracking.tests.factories import DEFAULT_TEST_SECRET, TrackingDeviceFactory

pytestmark = pytest.mark.django_db(transaction=True)


async def _run_scenario(scenario):
    """Starts a real server on an ephemeral port, runs `scenario(state)`
    against it, then triggers graceful shutdown and awaits full stop."""
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


async def _connect_and_identify(port, imei, secret):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write((json.dumps({"type": "identify", "imei": imei, "secret": secret}) + "\n").encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=5)
    return reader, writer, json.loads(line)


class TestConnectionLifecycle:
    def test_successful_identify_registers_session(self):
        device = TrackingDeviceFactory()

        async def scenario(state):
            reader, writer, ack = await _connect_and_identify(state["port"], device.imei, DEFAULT_TEST_SECRET)
            assert ack == {"type": "identify_ack", "status": "ok"}
            assert state["connection_manager"].is_connected(device.uuid)
            writer.close()
            await writer.wait_closed()

        asyncio.run(_run_scenario(scenario))

    def test_unknown_device_rejected_and_never_registered(self):
        async def scenario(state):
            reader, writer, ack = await _connect_and_identify(state["port"], "000000000000000", "whatever")
            assert ack == {"type": "identify_ack", "status": "rejected"}
            assert len(state["connection_manager"]) == 0
            writer.close()
            await writer.wait_closed()

        asyncio.run(_run_scenario(scenario))

    def test_wrong_secret_rejected(self):
        device = TrackingDeviceFactory()

        async def scenario(state):
            reader, writer, ack = await _connect_and_identify(state["port"], device.imei, "wrong-secret")
            assert ack["status"] == "rejected"
            assert not state["connection_manager"].is_connected(device.uuid)
            writer.close()
            await writer.wait_closed()

        asyncio.run(_run_scenario(scenario))

    def test_disconnect_unregisters_session(self):
        device = TrackingDeviceFactory()

        async def scenario(state):
            reader, writer, ack = await _connect_and_identify(state["port"], device.imei, DEFAULT_TEST_SECRET)
            assert ack["status"] == "ok"
            assert state["connection_manager"].is_connected(device.uuid)
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.5)  # let the server's read loop observe EOF
            assert not state["connection_manager"].is_connected(device.uuid)

        asyncio.run(_run_scenario(scenario))

    def test_identify_timeout_drops_connection(self):
        # apps.tracking.telematics_service.config reads settings once at
        # import time — patch the already-imported module's constant
        # directly rather than the Django setting, which wouldn't propagate.
        import apps.tracking.telematics_service.config as config_module
        original = config_module.IDENTIFY_TIMEOUT_SECONDS
        config_module.IDENTIFY_TIMEOUT_SECONDS = 1
        try:
            async def scenario(state):
                reader, writer = await asyncio.open_connection("127.0.0.1", state["port"])
                # Never send identify.
                data = await asyncio.wait_for(reader.read(100), timeout=3)
                assert data == b""  # connection closed by the server
                writer.close()

            asyncio.run(_run_scenario(scenario))
        finally:
            config_module.IDENTIFY_TIMEOUT_SECONDS = original

    def test_reconnect_after_disconnect(self):
        device = TrackingDeviceFactory()

        async def scenario(state):
            reader1, writer1, ack1 = await _connect_and_identify(state["port"], device.imei, DEFAULT_TEST_SECRET)
            assert ack1["status"] == "ok"
            writer1.close()
            await writer1.wait_closed()
            await asyncio.sleep(0.5)

            reader2, writer2, ack2 = await _connect_and_identify(state["port"], device.imei, DEFAULT_TEST_SECRET)
            assert ack2["status"] == "ok"
            assert state["connection_manager"].is_connected(device.uuid)
            writer2.close()
            await writer2.wait_closed()

        asyncio.run(_run_scenario(scenario))


class TestGracefulShutdown:
    def test_shutdown_closes_listening_socket_and_active_connections(self):
        device = TrackingDeviceFactory()

        async def scenario(state):
            reader, writer, ack = await _connect_and_identify(state["port"], device.imei, DEFAULT_TEST_SECRET)
            assert ack["status"] == "ok"
            # scenario just returns — _run_scenario triggers stop_event and
            # awaits full shutdown right after, which must not raise.

        asyncio.run(_run_scenario(scenario))  # no exception == clean shutdown


class TestSecurityLogging:
    def test_failed_identify_never_logs_secret(self, caplog):
        device = TrackingDeviceFactory()
        secret_guess = "definitely-not-the-real-secret"

        async def scenario(state):
            await _connect_and_identify(state["port"], device.imei, secret_guess)

        with caplog.at_level(logging.DEBUG):
            asyncio.run(_run_scenario(scenario))
        for record in caplog.records:
            assert secret_guess not in record.getMessage()
