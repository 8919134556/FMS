"""DeviceCommand claiming, dispatch, and timeout/retry sweeping.

The database is the ONLY coordination mechanism between Django (which
creates commands) and this service (which sends them) — they are separate
processes, so nothing here assumes an in-memory call from Django. All
claiming uses ``select_for_update(skip_locked=True)`` so a second worker
(or a future accidental second instance) can never double-claim or
double-send the same command.
"""

import asyncio
import logging

from django.db import transaction
from django.utils import timezone

from apps.tracking import command_services
from apps.tracking.models import DeviceCommand
from apps.tracking.telematics_service import command_encoding, config
from apps.tracking.telematics_service.db import run_sync

logger = logging.getLogger(__name__)


def _claim_and_queue_sync(device_ids):
    """ONE query for every connected device's PENDING commands — never
    polls per-device. Transitions each claimed row to QUEUED inside the
    same transaction that locked it, so a concurrent claimer (skip_locked)
    can never see it as PENDING again.

    Excludes any device that already has a SENT (unacknowledged) command:
    this keeps at most one command in flight per device, which matters for
    protocols with no wire-level response-correlation ID (Teltonika's
    Codec 12 — see command_dispatch.handle_teltonika_response) and is
    harmless, arguably safer, for the generic protocol too.
    """
    if not device_ids:
        return []
    with transaction.atomic():
        devices_with_in_flight_command = DeviceCommand.objects.filter(
            status=DeviceCommand.Status.SENT
        ).values("device_id")
        claimed = list(
            DeviceCommand.objects.select_for_update(skip_locked=True)
            .filter(device_id__in=device_ids, status=DeviceCommand.Status.PENDING)
            .exclude(device_id__in=devices_with_in_flight_command)
            .select_related("device")
            .order_by("created_at")
        )
        for command in claimed:
            command_services.transition_command(command=command, new_status=DeviceCommand.Status.QUEUED)
    return claimed


def _revert_to_pending_sync(command_pk):
    command = DeviceCommand.objects.select_for_update(skip_locked=True).filter(pk=command_pk).first()
    if command is None:
        return
    with transaction.atomic():
        if command.can_transition_to(DeviceCommand.Status.PENDING):
            command_services.transition_command(command=command, new_status=DeviceCommand.Status.PENDING)


def _mark_sent_sync(command_pk):
    with transaction.atomic():
        command = DeviceCommand.objects.select_for_update(skip_locked=True).filter(pk=command_pk).first()
        if command is not None and command.can_transition_to(DeviceCommand.Status.SENT):
            command_services.transition_command(command=command, new_status=DeviceCommand.Status.SENT)


def _mark_failed_sync(command_pk, error_message):
    """Used when a command has no valid encoding for its device's provider
    — retrying would never help, so this goes straight to FAILED (via
    QUEUED, its current state) rather than back to PENDING."""
    with transaction.atomic():
        command = DeviceCommand.objects.select_for_update(skip_locked=True).filter(pk=command_pk).first()
        if command is not None and command.can_transition_to(DeviceCommand.Status.FAILED):
            command_services.transition_command(
                command=command, new_status=DeviceCommand.Status.FAILED, last_error=error_message,
            )


def _sweep_timeouts_and_expiry_sync():
    """Two independent sweeps, both under skip_locked:
    1. SENT commands past the ACK timeout (sent_at-based) — retry if under
       max_retries, else EXPIRED.
    2. PENDING/QUEUED commands past a human-set expires_at — EXPIRED
       unconditionally, regardless of retry count.
    """
    now = timezone.now()
    ack_cutoff = now - timezone.timedelta(seconds=config.COMMAND_ACK_TIMEOUT_SECONDS)

    with transaction.atomic():
        timed_out = list(
            DeviceCommand.objects.select_for_update(skip_locked=True)
            .filter(status=DeviceCommand.Status.SENT, sent_at__lt=ack_cutoff)
        )
        for command in timed_out:
            if command.retry_count < command.max_retries:
                command_services.transition_command(
                    command=command, new_status=DeviceCommand.Status.PENDING,
                    retry_count=command.retry_count + 1,
                    last_error=f"ACK timeout (retry {command.retry_count + 1}/{command.max_retries}).",
                )
            else:
                command_services.transition_command(
                    command=command, new_status=DeviceCommand.Status.EXPIRED,
                    last_error="ACK timeout, max retries exceeded.",
                )

    with transaction.atomic():
        expired_by_deadline = list(
            DeviceCommand.objects.select_for_update(skip_locked=True).filter(
                status__in=[DeviceCommand.Status.PENDING, DeviceCommand.Status.QUEUED],
                expires_at__isnull=False,
                expires_at__lt=now,
            )
        )
        for command in expired_by_deadline:
            command_services.transition_command(
                command=command, new_status=DeviceCommand.Status.EXPIRED, last_error="Expired before delivery.",
            )


def _release_stuck_queued_sync():
    """Startup-only recovery: nothing could have survived a full process
    restart in the in-memory connection registry, so any QUEUED command
    (claimed but who knows if it was actually sent) is safely re-queued as
    PENDING rather than left stuck forever."""
    with transaction.atomic():
        stuck = list(
            DeviceCommand.objects.select_for_update(skip_locked=True).filter(status=DeviceCommand.Status.QUEUED)
        )
        for command in stuck:
            command_services.transition_command(command=command, new_status=DeviceCommand.Status.PENDING)
    if stuck:
        logger.info("Released %d QUEUED command(s) back to PENDING on startup.", len(stuck))


async def _dispatch_claimed(commands, connection_manager):
    for command in commands:
        session = connection_manager.get(command.device.uuid)
        if session is None:
            # Device disconnected between claim and send — safe revert, no error recorded.
            await run_sync(_revert_to_pending_sync, command.pk)
            continue

        try:
            encoded = command_encoding.encode_command_for_provider(session.provider, command)
        except command_encoding.UnsupportedCommandError as exc:
            logger.warning("Command %s not encodable for provider %s: %s", command.uuid, session.provider, exc)
            await run_sync(_mark_failed_sync, command.pk, str(exc))
            continue

        try:
            await session.send_bytes(encoded)
        except Exception:
            logger.exception("Failed to send command %s to device %s.", command.uuid, command.device.imei)
            await run_sync(_revert_to_pending_sync, command.pk)
        else:
            await run_sync(_mark_sent_sync, command.pk)


async def dispatch_to_one_device(device_uuid, connection_manager):
    """Called right after a device's successful identify — "check
    immediately when a device connects", per spec."""
    session = connection_manager.get(device_uuid)
    if session is None:
        return
    claimed = await run_sync(_claim_and_queue_sync, [session.device.pk])
    if claimed:
        await _dispatch_claimed(claimed, connection_manager)


async def run_periodic_tick(connection_manager):
    """The main coordination loop: claim+dispatch for every connected
    device in one query, then sweep timeouts/expiry. Runs forever until
    cancelled by the server's shutdown sequence."""
    while True:
        await asyncio.sleep(config.COMMAND_POLL_INTERVAL_SECONDS)
        try:
            device_ids = connection_manager.connected_device_ids()
            claimed = await run_sync(_claim_and_queue_sync, device_ids)
            if claimed:
                await _dispatch_claimed(claimed, connection_manager)
            await run_sync(_sweep_timeouts_and_expiry_sync)
        except Exception:
            logger.exception("Error in telematics command poll tick — continuing.")


async def handle_ack(command_id, status, response_payload=None):
    """Correlates an ACK by the command's own uuid. A stale/duplicate ACK
    (command not currently SENT) is silently ignored — never re-processed."""
    command = await run_sync(_get_sent_command_sync, command_id)
    if command is None:
        return
    await run_sync(command_services.transition_command, command=command, new_status=DeviceCommand.Status.ACKNOWLEDGED)
    terminal_status = DeviceCommand.Status.COMPLETED if status == "ok" else DeviceCommand.Status.FAILED
    await run_sync(
        command_services.record_terminal_outcome,
        command=command, new_status=terminal_status, response_payload=response_payload,
        last_error="" if status == "ok" else "Device reported an error.",
    )


def _get_sent_command_sync(command_id):
    return DeviceCommand.objects.filter(uuid=command_id, status=DeviceCommand.Status.SENT).select_related("device").first()


async def handle_teltonika_response(device, response_text):
    """Codec 12 has no request/response correlation ID on the wire — this
    correlates by "the oldest currently-SENT command for this device"
    instead, which is safe and deterministic because _claim_and_queue_sync
    never lets a device have more than one SENT command at a time. This is
    a persisted DB fact, not in-memory connection state, so it survives a
    service restart exactly like the ID-correlated generic path does."""
    command = await run_sync(_get_oldest_sent_command_for_device_sync, device.pk)
    if command is None:
        return
    await run_sync(command_services.transition_command, command=command, new_status=DeviceCommand.Status.ACKNOWLEDGED)
    # The base Codec 12 protocol has no standard success/failure indicator
    # beyond "the device replied" — treated as COMPLETED on arrival rather
    # than guessing at response-text semantics that aren't documented.
    await run_sync(
        command_services.record_terminal_outcome,
        command=command, new_status=DeviceCommand.Status.COMPLETED,
        response_payload={"response_text": response_text},
    )


def _get_oldest_sent_command_for_device_sync(device_pk):
    return (
        DeviceCommand.objects.filter(device_id=device_pk, status=DeviceCommand.Status.SENT)
        .order_by("sent_at")
        .first()
    )
