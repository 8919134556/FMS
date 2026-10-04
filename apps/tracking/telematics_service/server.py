"""The asyncio TCP accept loop(s) and per-connection message handling.

    Device connects -> identify -> TrackingDevice resolved
        -> session registered -> telemetry/heartbeat/ack messages handled
        -> disconnect -> session unregistered

Two independent listeners share one ``ConnectionManager`` and one command
poll loop: the generic NDJSON protocol (Phase 3.5) and the real Teltonika
Codec 8/12 protocol (Phase 3.6, see
``apps.tracking.telematics_service.protocol.teltonika``). Binary Codec 8
framing is unambiguous from byte one, so it cannot safely share a port with
NDJSON without fragile first-byte sniffing — one port per protocol is the
same choice real multi-protocol GPS platforms make.

Each protocol is described by a small ``ProtocolAdapter`` (framer +
authentication + identify/telemetry ACK encoding) so ``handle_connection``
and ``_handle_message`` stay protocol-agnostic — neither needs an
``if provider == "teltonika"`` branch for anything except which handler
resolves an incoming ``ack`` message's correlation (§ command_dispatch).

Telemetry is handed straight to the EXISTING
``apps.tracking.services.TelemetryIngestionService.ingest`` — this file
never re-implements ingestion, validation, dedup, or current-position
logic, for either protocol. Heartbeats only update the in-memory session's
`last_seen_at`; they never touch
``TrackingDevice.last_communication``/``VehicleCurrentTelemetry`` — those
stay telemetry-triggered only, preserving the existing backend
connection-status business rules.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from apps.tracking.services import TelemetryIngestionService
from apps.tracking.telematics_service import command_dispatch, config
from apps.tracking.telematics_service.auth import authenticate_device_sync, authenticate_teltonika_device_sync
from apps.tracking.telematics_service.connection_manager import ConnectionManager
from apps.tracking.telematics_service.db import run_sync
from apps.tracking.telematics_service.device_session import DeviceSession
from apps.tracking.telematics_service.protocol import teltonika as teltonika_protocol
from apps.tracking.telematics_service.protocol.base import FrameError
from apps.tracking.telematics_service.protocol.generic import NDJSONFramer
from apps.tracking.telematics_service.protocol.teltonika import TeltonikaFramer

logger = logging.getLogger(__name__)


@dataclass
class ProtocolAdapter:
    name: str
    framer_factory: Callable[[], object]
    authenticate: Callable[[dict], object]  # sync: identify message dict -> TrackingDevice | None
    encode_identify_ack: Callable[[bool], bytes]
    # None means this protocol has no telemetry-level ACK today (generic).
    encode_telemetry_ack: Optional[Callable[[int], bytes]] = None


GENERIC_ADAPTER = ProtocolAdapter(
    name="generic",
    framer_factory=NDJSONFramer,
    authenticate=lambda msg: authenticate_device_sync(msg.get("imei"), msg.get("secret")),
    encode_identify_ack=lambda ok: json.dumps({"type": "identify_ack", "status": "ok" if ok else "rejected"}).encode() + b"\n",
)

TELTONIKA_ADAPTER = ProtocolAdapter(
    name="teltonika",
    framer_factory=TeltonikaFramer,
    authenticate=lambda msg: authenticate_teltonika_device_sync(msg.get("imei")),
    encode_identify_ack=teltonika_protocol.encode_login_response,
    encode_telemetry_ack=teltonika_protocol.encode_telemetry_ack,
)


async def _iter_messages(reader, framer):
    """Yields decoded message dicts as they complete, buffering internally
    across reads — one recv() is NOT one device message (TCP is a stream),
    for either protocol."""
    while True:
        data = await reader.read(4096)
        if not data:
            return
        for message in framer.feed(data):
            yield message


async def _handle_message(message: dict, session: DeviceSession, adapter: ProtocolAdapter):
    msg_type = message.get("type")
    if msg_type == "telemetry":
        inner_payload = {k: v for k, v in message.items() if k != "type"}
        result = await run_sync(TelemetryIngestionService.ingest, device=session.device, raw_payload=inner_payload)
        if adapter.encode_telemetry_ack is not None:
            await session.send_bytes(adapter.encode_telemetry_ack(result.accepted))
    elif msg_type == "heartbeat":
        pass  # session.touch() already called by the caller; no DB write
    elif msg_type == "ack":
        if adapter.name == "teltonika":
            await command_dispatch.handle_teltonika_response(session.device, message.get("response_text", ""))
        else:
            await command_dispatch.handle_ack(
                message.get("command_id"), message.get("status"), message.get("response_payload")
            )
    elif msg_type == "_malformed":
        logger.warning(
            "Malformed message from device %s (%s protocol): %s",
            session.device.imei, adapter.name, message.get("reason") or f"{message.get('raw_length', '?')} bytes",
        )
    else:
        logger.warning("Unknown message type %r from device %s", msg_type, session.device.imei)


async def handle_connection(reader, writer, connection_manager: ConnectionManager, adapter: ProtocolAdapter):
    peername = writer.get_extra_info("peername")
    framer = adapter.framer_factory()
    messages = _iter_messages(reader, framer)
    session = None

    async def _next(timeout):
        try:
            return await asyncio.wait_for(messages.__anext__(), timeout=timeout)
        except StopAsyncIteration:
            return None

    try:
        identify_message = await _next(config.IDENTIFY_TIMEOUT_SECONDS)
        if not identify_message or identify_message.get("type") != "identify":
            writer.write(adapter.encode_identify_ack(False))
            await writer.drain()
            return

        device = await run_sync(adapter.authenticate, identify_message)
        if device is None:
            # Never log the secret; never reveal WHY it was rejected
            # (unknown IMEI vs wrong secret are indistinguishable).
            logger.info("Identification failed for imei=%s from %s (%s)", identify_message.get("imei"), peername, adapter.name)
            writer.write(adapter.encode_identify_ack(False))
            await writer.drain()
            return

        session = DeviceSession(reader=reader, writer=writer, device=device, provider=adapter.name, peername=peername)
        connection_manager.register(session)
        writer.write(adapter.encode_identify_ack(True))
        await writer.drain()
        logger.info("Device %s identified from %s (%s)", device.imei, peername, adapter.name)

        await command_dispatch.dispatch_to_one_device(device.uuid, connection_manager)

        while True:
            message = await _next(config.CONNECTION_IDLE_TIMEOUT_SECONDS)
            if message is None:
                break
            session.touch()
            await _handle_message(message, session, adapter)

    except (asyncio.TimeoutError, FrameError, ConnectionError):
        pass
    except Exception:
        logger.exception("Unexpected error handling connection from %s (%s)", peername, adapter.name)
    finally:
        if session is not None:
            connection_manager.unregister(session)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def run_server(stop_event=None, host=None, generic_port=None, teltonika_port=None, on_ready=None):
    """Starts both TCP accept loops and the periodic command-dispatch tick.
    Runs until ``stop_event`` is set (or forever if not given — production
    use always provides one via the management command's signal handlers).

    ``host``/``generic_port``/``teltonika_port``/``on_ready`` exist purely
    for tests (ephemeral port 0, and a callback to learn which ports the OS
    actually assigned and to reach the shared ConnectionManager).
    """
    await run_sync(command_dispatch._release_stuck_queued_sync)

    connection_manager = ConnectionManager()

    async def _on_generic_connect(reader, writer):
        await handle_connection(reader, writer, connection_manager, GENERIC_ADAPTER)

    async def _on_teltonika_connect(reader, writer):
        await handle_connection(reader, writer, connection_manager, TELTONIKA_ADAPTER)

    bind_host = host or config.TCP_HOST
    generic_server = await asyncio.start_server(
        _on_generic_connect, bind_host, config.TCP_PORT if generic_port is None else generic_port
    )
    teltonika_server = await asyncio.start_server(
        _on_teltonika_connect, bind_host, config.TELTONIKA_TCP_PORT if teltonika_port is None else teltonika_port
    )

    if on_ready is not None:
        on_ready({"generic": generic_server, "teltonika": teltonika_server}, connection_manager)
    logger.info(
        "Telematics TCP server listening: generic=%s teltonika=%s",
        [s.getsockname() for s in generic_server.sockets],
        [s.getsockname() for s in teltonika_server.sockets],
    )

    poll_task = asyncio.create_task(command_dispatch.run_periodic_tick(connection_manager))

    try:
        async with generic_server, teltonika_server:
            serve_tasks = [
                asyncio.create_task(generic_server.serve_forever()),
                asyncio.create_task(teltonika_server.serve_forever()),
            ]
            if stop_event is not None:
                await stop_event.wait()
                for task in serve_tasks:
                    task.cancel()
                for task in serve_tasks:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            else:
                await asyncio.gather(*serve_tasks)
    finally:
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass
        for session in connection_manager.all_sessions():
            session.writer.close()
        logger.info("Telematics TCP server stopped.")
