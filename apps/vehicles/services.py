"""Business logic for vehicle-driver assignment.

Kept out of the view so the "end the previous primary assignment, then
create the new one, then update the vehicle's convenience fields" sequence
is one transaction with one obvious entry point, instead of logic scattered
across a view method.
"""

from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.vehicles.models import Vehicle, VehicleDriverAssignment


@transaction.atomic
def assign_driver(*, vehicle, driver, start_date, assignment_type, primary_driver, remarks, assigned_by, request=None):
    """Creates a new assignment, ending any existing active primary
    assignment for the same vehicle first so the unique constraint on
    (vehicle, status=ACTIVE, primary_driver=True) is never violated.
    """
    previous = None
    if primary_driver:
        previous = (
            VehicleDriverAssignment.objects.select_for_update()
            .filter(vehicle=vehicle, status=VehicleDriverAssignment.Status.ACTIVE, primary_driver=True)
            .first()
        )
        if previous:
            previous.status = VehicleDriverAssignment.Status.ENDED
            previous.end_date = start_date
            previous.updated_by = assigned_by
            previous.save(update_fields=["status", "end_date", "updated_by", "updated_at"])
            log_action(
                action=AuditLog.Action.UNASSIGN,
                module="assignment",
                entity="VehicleDriverAssignment",
                entity_id=str(previous.pk),
                old_value={"driver_id": previous.driver_id, "vehicle_id": vehicle.pk},
                user=assigned_by,
                request=request,
            )

    assignment = VehicleDriverAssignment.objects.create(
        vehicle=vehicle,
        driver=driver,
        assignment_type=assignment_type,
        start_date=start_date,
        primary_driver=primary_driver,
        remarks=remarks,
        assigned_by=assigned_by,
        created_by=assigned_by,
        updated_by=assigned_by,
    )

    if primary_driver:
        vehicle.current_driver = driver
        if vehicle.availability_status == Vehicle.AvailabilityStatus.AVAILABLE:
            vehicle.availability_status = Vehicle.AvailabilityStatus.ASSIGNED
        vehicle.updated_by = assigned_by
        vehicle.save(update_fields=["current_driver", "availability_status", "updated_by", "updated_at"])

    log_action(
        action=AuditLog.Action.ASSIGN,
        module="assignment",
        entity="VehicleDriverAssignment",
        entity_id=str(assignment.pk),
        new_value={"driver_id": driver.pk, "vehicle_id": vehicle.pk, "primary": primary_driver},
        user=assigned_by,
        request=request,
    )
    return assignment


@transaction.atomic
def end_assignment(*, assignment, ended_by, request=None):
    assignment.status = VehicleDriverAssignment.Status.ENDED
    assignment.end_date = assignment.end_date or timezone.now().date()
    assignment.updated_by = ended_by
    assignment.save(update_fields=["status", "end_date", "updated_by", "updated_at"])

    vehicle = assignment.vehicle
    if assignment.primary_driver and vehicle.current_driver_id == assignment.driver_id:
        vehicle.current_driver = None
        if vehicle.availability_status == Vehicle.AvailabilityStatus.ASSIGNED:
            vehicle.availability_status = Vehicle.AvailabilityStatus.AVAILABLE
        vehicle.updated_by = ended_by
        vehicle.save(update_fields=["current_driver", "availability_status", "updated_by", "updated_at"])

    log_action(
        action=AuditLog.Action.UNASSIGN,
        module="assignment",
        entity="VehicleDriverAssignment",
        entity_id=str(assignment.pk),
        old_value={"driver_id": assignment.driver_id, "vehicle_id": vehicle.pk},
        user=ended_by,
        request=request,
    )
    return assignment
