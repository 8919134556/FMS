"""Vehicle immobilizer ON/OFF through the Zentora comms command API.

Two statuses, kept apart on purpose:

* the COMMAND's progress — ImmobilizerCommand.status, from the comms API
  (POST /commands/immobilizer -> command_id, then GET /commands/{command_id}):
  0 Pending, 1 Processing, 2 Sent to device, 3 Failed, 4 Completed;
* the vehicle's ACTUAL state — VehicleCurrentTelemetry.immobilizer, from the
  device data (comms current_table "immobalizer", 1 = ON, 0 = OFF) via the comms
  bridge. Nothing in this module ever writes it: a "Completed" command is not
  proof the immobilizer changed.

The API is called server-side only (the browser talks to FMS, never to the
comms API), with the unit number of the vehicle's own GPS device. One command
in progress per vehicle at a time.
"""

import json
import logging
import re
import urllib.error
import urllib.request

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.tracking.models import ImmobilizerCommand, VehicleCurrentTelemetry

logger = logging.getLogger(__name__)

_UNIT_NO = re.compile(r"^[A-Za-z0-9-]{5,20}$")
STATUS_LABELS = dict(ImmobilizerCommand.Status.choices)


class ImmobilizerError(Exception):
    """A request FMS refuses or the comms API rejected — ``message`` is safe to show the user."""

    status_code = 400

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.message = message
        if status_code:
            self.status_code = status_code


class CommandApiError(ImmobilizerError):
    status_code = 502


def api_configured():
    return bool((settings.COMMS_COMMAND_API_URL or "").strip())


def _open(request, timeout):  # one seam for tests
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 — URL comes from settings


def _call(method, path, body=None):
    """JSON request to the comms command API; returns the decoded object or raises CommandApiError."""
    base = (settings.COMMS_COMMAND_API_URL or "").strip().rstrip("/")
    if not base:
        raise CommandApiError("The command service is not configured.", 503)
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if settings.COMMS_COMMAND_API_KEY:
        headers["X-API-Key"] = settings.COMMS_COMMAND_API_KEY
    request = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)
    try:
        with _open(request, settings.COMMS_COMMAND_API_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        logger.warning("Comms command API %s %s -> HTTP %s", method, path, exc.code)
        raise CommandApiError(f"The command service answered with an error (HTTP {exc.code}).") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("Comms command API %s %s unreachable: %s", method, path, exc)
        raise CommandApiError("The command service could not be reached.") from exc
    try:
        payload = json.loads(raw or b"null")
    except ValueError as exc:
        raise CommandApiError("The command service sent an invalid response.") from exc
    if not isinstance(payload, dict):
        raise CommandApiError("The command service sent an invalid response.")
    return payload


def unit_no_for(vehicle):
    """The unit number (IMEI) of the vehicle's own GPS device, or None when it has no usable one."""
    device = getattr(vehicle, "tracking_device", None)
    if device is None or device.is_deleted or device.status == device.Status.REMOVED:
        return None, None
    unit_no = (device.imei or "").strip()
    return (unit_no if _UNIT_NO.match(unit_no) else None), device


def _timeout_seconds():
    return max(10, int(settings.IMMOBILIZER_COMMAND_TIMEOUT_SECONDS))


def latest_command(vehicle):
    return ImmobilizerCommand.objects.filter(vehicle=vehicle).select_related("requested_by").first()


def _expire_if_overdue(command, now=None):
    now = now or timezone.now()
    if command.in_progress and (now - command.created_at).total_seconds() > _timeout_seconds():
        command.timed_out = True
        command.finished_at = now
        command.error = command.error or "No final status from the command service in time."
        command.save(update_fields=["timed_out", "finished_at", "error", "updated_at"])
    return command


def send(*, vehicle, state, user, request=None):
    """Ask the comms API to turn the vehicle's immobilizer ``state`` ("on"/"off").
    Returns the ImmobilizerCommand (status Pending, with the API's command_id).
    Raises ImmobilizerError for anything FMS refuses or the API rejects."""
    state = str(state or "").strip().lower()
    if state not in ImmobilizerCommand.State.values:
        raise ImmobilizerError('State must be "on" or "off".')
    if not api_configured():
        raise ImmobilizerError("The command service is not configured.", 503)
    unit_no, device = unit_no_for(vehicle)
    if not unit_no:
        raise ImmobilizerError("This vehicle has no GPS device with a valid unit number.")

    with transaction.atomic():
        # One command in progress per vehicle: lock its rows, retire an overdue one, refuse otherwise.
        running = (
            ImmobilizerCommand.objects.select_for_update()
            .filter(vehicle=vehicle, finished_at__isnull=True)
            .order_by("-created_at")
        )
        for command in running:
            _expire_if_overdue(command)
            if command.in_progress:
                raise ImmobilizerError("A command for this vehicle is still in progress.", 409)
        command = ImmobilizerCommand.objects.create(
            vehicle=vehicle, device=device, unit_no=unit_no, requested_state=state, requested_by=user,
        )

    # Outside the transaction: the in-progress row above already blocks a second submission.
    try:
        payload = _call("POST", "/commands/immobilizer", {"unit_no": unit_no, "state": state})
        command_id = payload.get("command_id")
        if str(payload.get("status", "")).lower() != "success" or not isinstance(command_id, int) or command_id < 0:
            raise CommandApiError("The command service did not accept the command.")
    except ImmobilizerError as exc:
        command.error = exc.message[:255]
        command.finished_at = timezone.now()
        command.save(update_fields=["error", "finished_at", "updated_at"])
        _audit(command, user, request, accepted=False)
        raise
    command.command_id = command_id
    command.status = ImmobilizerCommand.Status.PENDING
    command.status_name = "pending"
    command.save(update_fields=["command_id", "status", "status_name", "updated_at"])
    _audit(command, user, request, accepted=True)
    return command


def refresh(command):
    """Ask the comms API for the command's status (GET /commands/{command_id}) while it is in
    progress. Returns (command, transient_error_message_or_None). A network hiccup is reported
    but never marks the command failed; the timeout ends it."""
    if not command.in_progress or command.command_id is None:
        return command, None
    try:
        payload = _call("GET", f"/commands/{command.command_id}")
    except ImmobilizerError as exc:
        _expire_if_overdue(command)
        return command, exc.message
    # Only a reply about THIS command for THIS vehicle's unit counts.
    if payload.get("command_id") != command.command_id or str(payload.get("unit_no", "")).strip() != command.unit_no:
        logger.warning("Comms command %s: reply for another command/unit ignored", command.command_id)
        _expire_if_overdue(command)
        return command, "The command service returned a status for a different command."
    status = payload.get("status")
    if not isinstance(status, int) or status not in STATUS_LABELS:
        _expire_if_overdue(command)
        return command, "The command service returned an unknown status."
    now = timezone.now()
    command.status = status
    command.status_name = str(payload.get("status_name") or "")[:40]
    command.last_checked_at = now
    fields = ["status", "status_name", "last_checked_at", "updated_at"]
    if status in ImmobilizerCommand.TERMINAL_STATUSES:
        command.finished_at = now
        fields.append("finished_at")
        if status == ImmobilizerCommand.Status.FAILED:
            command.error = "The device did not carry out the command."
            fields.append("error")
    command.save(update_fields=fields)
    _expire_if_overdue(command, now)
    return command, None


def _audit(command, user, request, *, accepted):
    log_action(
        action=AuditLog.Action.CREATE, module="tracking_device", entity="ImmobilizerCommand",
        entity_id=str(command.pk),
        new_value={
            "vehicle": command.vehicle.registration_number, "unit_no": command.unit_no,
            "state": command.requested_state, "command_id": command.command_id, "accepted": accepted,
            **({} if accepted else {"error": command.error}),
        },
        user=user, request=request,
    )


def actual_state(vehicle):
    """The vehicle's ACTUAL immobilizer state from its current telemetry: True/False/None."""
    row = VehicleCurrentTelemetry.objects.filter(vehicle=vehicle).only("immobilizer", "timestamp").first()
    return (row.immobilizer, row.timestamp) if row else (None, None)


def serialize(command):
    if command is None:
        return None
    user = command.requested_by
    return {
        "uuid": str(command.uuid),
        "command_id": command.command_id,
        "unit_no": command.unit_no,
        "requested_state": command.requested_state,
        "status": command.status,
        "status_label": STATUS_LABELS.get(command.status) if command.status is not None else None,
        "status_name": command.status_name,
        "in_progress": command.in_progress,
        "timed_out": command.timed_out,
        "error": command.error,
        "requested_by": (user.get_full_name() or user.username) if user else "",
        "created_at": command.created_at.isoformat(),
        "finished_at": command.finished_at.isoformat() if command.finished_at else None,
        "timeout_seconds": _timeout_seconds(),
    }
