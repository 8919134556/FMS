"""Maintenance lifecycle business logic: number generation, state
transitions, and the Vehicle/Trip integration points.

Kept out of the views/forms for the same reason apps.trips.services and
apps.vehicles.services are — "validate, then commit atomically, then log"
as one obvious entry point per transition.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.maintenance.models import Maintenance, MaintenanceNumberSequence
from apps.trips.models import Trip
from apps.vehicles.models import Vehicle


def generate_maintenance_number():
    with transaction.atomic():
        seq, _ = MaintenanceNumberSequence.objects.select_for_update().get_or_create(id=1)
        seq.last_value += 1
        seq.save(update_fields=["last_value"])
        return f"MNT-{seq.last_value:06d}"


def _revert_vehicle_after_maintenance(vehicle, actor):
    """Mirrors apps.trips.services._revert_vehicle_availability — puts the
    vehicle back to ASSIGNED (has a primary driver) or AVAILABLE once it's
    no longer actively under service."""
    vehicle.status = Vehicle.Status.ACTIVE
    vehicle.availability_status = (
        Vehicle.AvailabilityStatus.ASSIGNED if vehicle.current_driver_id else Vehicle.AvailabilityStatus.AVAILABLE
    )
    vehicle.updated_by = actor
    vehicle.save(update_fields=["status", "availability_status", "updated_by", "updated_at"])


@transaction.atomic
def start_maintenance(*, maintenance, started_by, request=None):
    if not maintenance.can_transition_to(Maintenance.Status.IN_PROGRESS):
        raise ValidationError(
            f"Maintenance can't be started from its current status ({maintenance.get_status_display()})."
        )

    vehicle = maintenance.vehicle
    has_active_trip = Trip.objects.filter(vehicle=vehicle, status__in=Trip.ACTIVE_ASSIGNMENT_STATUSES).exists()
    if has_active_trip:
        raise ValidationError("Vehicle currently has an active trip and cannot enter maintenance.")

    maintenance.started_date = timezone.now()
    maintenance.status = Maintenance.Status.IN_PROGRESS
    maintenance.updated_by = started_by
    maintenance.save(update_fields=["started_date", "status", "updated_by", "updated_at"])

    vehicle.status = Vehicle.Status.UNDER_MAINTENANCE
    vehicle.availability_status = Vehicle.AvailabilityStatus.MAINTENANCE
    vehicle.updated_by = started_by
    vehicle.save(update_fields=["status", "availability_status", "updated_by", "updated_at"])

    log_action(
        action=AuditLog.Action.START, module="maintenance", entity="Maintenance", entity_id=str(maintenance.pk),
        new_value={"status": maintenance.status, "started_date": maintenance.started_date.isoformat()},
        user=started_by, request=request,
    )
    return maintenance


@transaction.atomic
def complete_maintenance(
    *, maintenance, completed_date, final_odometer, actual_cost, labor_cost,
    work_performed, completion_notes, next_service_date, next_service_odometer,
    completed_by, request=None,
):
    if not maintenance.can_transition_to(Maintenance.Status.COMPLETED):
        raise ValidationError(
            f"Maintenance can't be completed from its current status ({maintenance.get_status_display()})."
        )

    maintenance.status = Maintenance.Status.COMPLETED
    maintenance.completed_date = completed_date
    maintenance.actual_cost = actual_cost
    maintenance.labor_cost = labor_cost
    maintenance.work_performed = work_performed
    maintenance.completion_notes = completion_notes
    maintenance.next_service_date = next_service_date
    maintenance.next_service_odometer = next_service_odometer
    maintenance.updated_by = completed_by
    maintenance.save(
        update_fields=[
            "status", "completed_date", "actual_cost", "labor_cost", "work_performed",
            "completion_notes", "next_service_date", "next_service_odometer", "updated_by", "updated_at",
        ]
    )

    vehicle = maintenance.vehicle
    vehicle.last_service_date = completed_date.date() if hasattr(completed_date, "date") else completed_date
    if final_odometer is not None:
        vehicle.last_service_odometer = final_odometer
        if vehicle.odometer_reading is None or final_odometer > vehicle.odometer_reading:
            vehicle.odometer_reading = final_odometer
    if next_service_date:
        vehicle.next_service_date = next_service_date
    if next_service_odometer:
        vehicle.next_service_odometer = next_service_odometer
    vehicle.status = Vehicle.Status.ACTIVE
    vehicle.availability_status = (
        Vehicle.AvailabilityStatus.ASSIGNED if vehicle.current_driver_id else Vehicle.AvailabilityStatus.AVAILABLE
    )
    vehicle.updated_by = completed_by
    vehicle.save(
        update_fields=[
            "last_service_date", "last_service_odometer", "next_service_date", "next_service_odometer",
            "odometer_reading", "status", "availability_status", "updated_by", "updated_at",
        ]
    )

    log_action(
        action=AuditLog.Action.COMPLETE, module="maintenance", entity="Maintenance", entity_id=str(maintenance.pk),
        new_value={"actual_cost": str(actual_cost) if actual_cost is not None else None},
        user=completed_by, request=request,
    )
    return maintenance


@transaction.atomic
def cancel_maintenance(*, maintenance, cancellation_reason, cancelled_by, request=None):
    if not maintenance.can_transition_to(Maintenance.Status.CANCELLED):
        raise ValidationError(
            f"Maintenance can't be cancelled from its current status ({maintenance.get_status_display()})."
        )
    if not cancellation_reason or not cancellation_reason.strip():
        raise ValidationError("A cancellation reason is required.")

    was_in_progress = maintenance.status == Maintenance.Status.IN_PROGRESS
    maintenance.status = Maintenance.Status.CANCELLED
    maintenance.cancellation_reason = cancellation_reason
    maintenance.updated_by = cancelled_by
    maintenance.save(update_fields=["status", "cancellation_reason", "updated_by", "updated_at"])

    if was_in_progress:
        _revert_vehicle_after_maintenance(maintenance.vehicle, cancelled_by)

    log_action(
        action=AuditLog.Action.CANCEL, module="maintenance", entity="Maintenance", entity_id=str(maintenance.pk),
        new_value={"cancellation_reason": cancellation_reason},
        user=cancelled_by, request=request,
    )
    return maintenance
