"""DeviceCommand lifecycle — the sync ORM boundary for command state.

Mirrors apps.tracking.services' style: "validate, then commit atomically,
then log" in one obvious place per operation. This module is imported by
BOTH the Django views (human-issued create/cancel/retry) and the async
telematics_service package (system-driven transitions, via
apps.tracking.telematics_service.db.run_sync) — it has no knowledge of
either caller, it's pure Django ORM + audit logging.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.tracking.models import DeviceCommand

_TERMINAL_STATUSES = {
    DeviceCommand.Status.COMPLETED,
    DeviceCommand.Status.FAILED,
    DeviceCommand.Status.EXPIRED,
    DeviceCommand.Status.CANCELLED,
}

_TIMESTAMP_FIELD_FOR_STATUS = {
    DeviceCommand.Status.QUEUED: "queued_at",
    DeviceCommand.Status.SENT: "sent_at",
    DeviceCommand.Status.ACKNOWLEDGED: "acknowledged_at",
}


@transaction.atomic
def transition_command(*, command, new_status, actor=None, **field_updates):
    """The ONLY place DeviceCommand.status is ever assigned outside a
    migration. Validates the transition, auto-stamps the matching lifecycle
    timestamp, stamps completed_at on ANY terminal status (not just
    COMPLETED), applies any extra field updates, and saves explicitly."""
    if not command.can_transition_to(new_status):
        raise ValidationError(
            f"Command can't move from {command.get_status_display()} to {DeviceCommand.Status(new_status).label}."
        )

    update_fields = ["status", "updated_at"]
    command.status = new_status

    timestamp_field = _TIMESTAMP_FIELD_FOR_STATUS.get(new_status)
    if timestamp_field and not getattr(command, timestamp_field):
        setattr(command, timestamp_field, timezone.now())
        update_fields.append(timestamp_field)

    if new_status in _TERMINAL_STATUSES:
        command.completed_at = timezone.now()
        update_fields.append("completed_at")

    for field_name, value in field_updates.items():
        setattr(command, field_name, value)
        if field_name not in update_fields:
            update_fields.append(field_name)

    command.save(update_fields=update_fields)
    return command


def create_command(*, device, command_type, payload=None, expires_at=None, actor, request=None):
    command = DeviceCommand.objects.create(
        device=device,
        command_type=command_type,
        payload=payload or {},
        expires_at=expires_at,
        created_by=actor,
    )
    log_action(
        action=AuditLog.Action.CREATE, module="tracking_device", entity="DeviceCommand", entity_id=str(command.pk),
        new_value={"device_id": device.pk, "command_type": command_type},
        user=actor, request=request,
    )
    return command


def cancel_command(*, command, actor, request=None):
    if command.status not in (DeviceCommand.Status.PENDING, DeviceCommand.Status.QUEUED):
        raise ValidationError(f"A {command.get_status_display()} command can't be cancelled.")
    transition_command(command=command, new_status=DeviceCommand.Status.CANCELLED, actor=actor)
    log_action(
        action=AuditLog.Action.CANCEL, module="tracking_device", entity="DeviceCommand", entity_id=str(command.pk),
        user=actor, request=request,
    )
    return command


def retry_command(*, command, actor, request=None):
    if command.status not in (DeviceCommand.Status.FAILED, DeviceCommand.Status.EXPIRED):
        raise ValidationError(f"A {command.get_status_display()} command can't be retried.")
    if command.retry_count >= command.max_retries:
        raise ValidationError("This command has already used all of its retry attempts.")
    transition_command(
        command=command, new_status=DeviceCommand.Status.PENDING, actor=actor,
        retry_count=command.retry_count + 1, last_error="",
    )
    log_action(
        action=AuditLog.Action.UPDATE, module="tracking_device", entity="DeviceCommand", entity_id=str(command.pk),
        new_value={"retry_count": command.retry_count}, user=actor, request=request,
    )
    return command


def record_terminal_outcome(*, command, new_status, response_payload=None, last_error=""):
    """System-driven (no human actor) — used by the ACK handler to resolve
    ACKNOWLEDGED -> COMPLETED/FAILED. Reuses existing AuditLog.Action values
    (COMPLETE / UPDATE) rather than adding a new one."""
    transition_command(
        command=command, new_status=new_status,
        response_payload=response_payload, last_error=last_error,
    )
    audit_action = AuditLog.Action.COMPLETE if new_status == DeviceCommand.Status.COMPLETED else AuditLog.Action.UPDATE
    log_action(
        action=audit_action, module="tracking_device", entity="DeviceCommand", entity_id=str(command.pk),
        new_value={"status": new_status}, user=None,
    )
    return command
