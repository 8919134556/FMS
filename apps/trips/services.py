"""Trip lifecycle business logic: number generation, state transitions, and
the vehicle/driver overlap-conflict checks that back the Assign workflow.

Kept out of the views/forms for the same reason apps.vehicles.services is —
"validate everything, then commit atomically, then log" is one obvious
entry point per transition instead of logic scattered across view methods.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.drivers.models import Driver
from apps.trips.models import Trip, TripNumberSequence
from apps.vehicles.models import Vehicle


def trip_status_counts(queryset):
    """One grouped-count query (same idiom as apps.core.views' dashboard)
    powering both the list page's KPI row and its quick-filter pill bar —
    never one query per status. ``queryset`` should already have every
    non-status filter applied (search/client/vehicle/driver/date/etc.) so
    the counts reflect "what would this pill show if I clicked it", not a
    global total unrelated to the operator's current search."""
    counts = dict(queryset.values_list("status").annotate(count=Count("id")))
    return {choice: counts.get(choice, 0) for choice, _ in Trip.Status.choices}


def generate_trip_number():
    """Race-safe under concurrent creates: the row lock on the singleton
    sequence row serializes number issuance instead of racing on
    ``Trip.objects.count()``, which two concurrent requests could both read
    as the same value."""
    with transaction.atomic():
        seq, _ = TripNumberSequence.objects.select_for_update().get_or_create(id=1)
        seq.last_value += 1
        seq.save(update_fields=["last_value"])
        return f"TRP-{seq.last_value:06d}"


def _overlap_qs(field, resource, start, end, exclude_trip_id, lock):
    qs = Trip.objects.filter(
        **{field: resource},
        status__in=Trip.ACTIVE_ASSIGNMENT_STATUSES,
        scheduled_start__lt=end,
        scheduled_end__gt=start,
    )
    if exclude_trip_id:
        qs = qs.exclude(pk=exclude_trip_id)
    if lock:
        qs = qs.select_for_update()
    return qs.select_related("client").order_by("scheduled_start")


def find_vehicle_conflict(vehicle, start, end, exclude_trip_id=None, lock=False):
    return _overlap_qs("vehicle", vehicle, start, end, exclude_trip_id, lock).first()


def find_driver_conflict(driver, start, end, exclude_trip_id=None, lock=False):
    return _overlap_qs("driver", driver, start, end, exclude_trip_id, lock).first()


def _revert_vehicle_availability(vehicle, actor):
    """After a trip stops actively using a vehicle (completed/cancelled),
    put it back to ASSIGNED (still has a primary driver) or AVAILABLE —
    mirrors the same fallback rule apps.vehicles.services.end_assignment
    uses when a driver assignment ends."""
    if not vehicle:
        return
    new_status = (
        Vehicle.AvailabilityStatus.ASSIGNED if vehicle.current_driver_id else Vehicle.AvailabilityStatus.AVAILABLE
    )
    if vehicle.availability_status != new_status:
        vehicle.availability_status = new_status
        vehicle.updated_by = actor
        vehicle.save(update_fields=["availability_status", "updated_by", "updated_at"])


@transaction.atomic
def schedule_trip(*, trip, scheduled_by, request=None):
    """Promotes a Draft trip to Scheduled — the only way a draft moves
    forward, since "Save Draft" on the create form is otherwise a dead end."""
    if not trip.can_transition_to(Trip.Status.SCHEDULED):
        raise ValidationError(f"Trip can't be scheduled from its current status ({trip.get_status_display()}).")

    trip.status = Trip.Status.SCHEDULED
    trip.updated_by = scheduled_by
    trip.save(update_fields=["status", "updated_by", "updated_at"])

    log_action(
        action=AuditLog.Action.UPDATE, module="trip", entity="Trip", entity_id=str(trip.pk),
        new_value={"status": trip.status}, user=scheduled_by, request=request,
    )
    return trip


@transaction.atomic
def assign_trip(*, trip, vehicle, driver, assigned_by, request=None):
    if not trip.can_transition_to(Trip.Status.ASSIGNED):
        raise ValidationError(f"Trip can't be assigned from its current status ({trip.get_status_display()}).")

    if vehicle.status != Vehicle.Status.ACTIVE:
        raise ValidationError(f"{vehicle.registration_number} is not active and can't be assigned to a trip.")
    if vehicle.client_id and vehicle.client_id != trip.client_id:
        raise ValidationError(f"{vehicle.registration_number} belongs to a different client than this trip.")

    if driver.employment_status != Driver.EmploymentStatus.ACTIVE:
        raise ValidationError(f"{driver.get_full_name()} is not an active driver.")
    if driver.license_expiry_status == "EXPIRED":
        raise ValidationError(f"{driver.get_full_name()}'s license has expired and can't be assigned to a trip.")
    if driver.client_id and driver.client_id != trip.client_id:
        raise ValidationError(f"{driver.get_full_name()} belongs to a different client than this trip.")

    vehicle_conflict = find_vehicle_conflict(
        vehicle, trip.scheduled_start, trip.scheduled_end, exclude_trip_id=trip.pk, lock=True
    )
    if vehicle_conflict:
        raise ValidationError(
            f"{vehicle.registration_number} is already assigned to trip {vehicle_conflict.trip_number} "
            f"during this time window."
        )
    driver_conflict = find_driver_conflict(
        driver, trip.scheduled_start, trip.scheduled_end, exclude_trip_id=trip.pk, lock=True
    )
    if driver_conflict:
        raise ValidationError(
            f"{driver.get_full_name()} is already assigned to trip {driver_conflict.trip_number} "
            f"during this time window."
        )

    trip.vehicle = vehicle
    trip.driver = driver
    trip.status = Trip.Status.ASSIGNED
    trip.updated_by = assigned_by
    trip.save(update_fields=["vehicle", "driver", "status", "updated_by", "updated_at"])

    log_action(
        action=AuditLog.Action.ASSIGN, module="trip", entity="Trip", entity_id=str(trip.pk),
        new_value={"vehicle_id": vehicle.pk, "driver_id": driver.pk},
        user=assigned_by, request=request,
    )
    return trip


@transaction.atomic
def dispatch_trip(*, trip, dispatched_by, request=None):
    if not trip.can_transition_to(Trip.Status.DISPATCHED):
        raise ValidationError(f"Trip can't be dispatched from its current status ({trip.get_status_display()}).")

    trip.status = Trip.Status.DISPATCHED
    trip.updated_by = dispatched_by
    trip.save(update_fields=["status", "updated_by", "updated_at"])

    if trip.vehicle_id:
        trip.vehicle.availability_status = Vehicle.AvailabilityStatus.ON_TRIP
        trip.vehicle.updated_by = dispatched_by
        trip.vehicle.save(update_fields=["availability_status", "updated_by", "updated_at"])

    log_action(
        action=AuditLog.Action.DISPATCH, module="trip", entity="Trip", entity_id=str(trip.pk),
        new_value={"status": trip.status}, user=dispatched_by, request=request,
    )
    return trip


@transaction.atomic
def start_trip(*, trip, started_by, request=None):
    if not trip.can_transition_to(Trip.Status.IN_PROGRESS):
        raise ValidationError(f"Trip can't be started from its current status ({trip.get_status_display()}).")

    trip.actual_start = timezone.now()
    trip.status = Trip.Status.IN_PROGRESS
    trip.updated_by = started_by
    trip.save(update_fields=["actual_start", "status", "updated_by", "updated_at"])

    log_action(
        action=AuditLog.Action.START, module="trip", entity="Trip", entity_id=str(trip.pk),
        new_value={"actual_start": trip.actual_start.isoformat()}, user=started_by, request=request,
    )
    return trip


@transaction.atomic
def delay_trip(*, trip, delay_reason, delay_notes, delayed_by, request=None):
    if not trip.can_transition_to(Trip.Status.DELAYED):
        raise ValidationError(f"Trip can't be marked delayed from its current status ({trip.get_status_display()}).")

    trip.status = Trip.Status.DELAYED
    trip.delay_reason = delay_reason
    trip.delay_notes = delay_notes
    trip.updated_by = delayed_by
    trip.save(update_fields=["status", "delay_reason", "delay_notes", "updated_by", "updated_at"])

    log_action(
        action=AuditLog.Action.DELAY, module="trip", entity="Trip", entity_id=str(trip.pk),
        new_value={"delay_reason": delay_reason, "delay_notes": delay_notes}, user=delayed_by, request=request,
    )
    return trip


@transaction.atomic
def resume_trip(*, trip, resumed_by, request=None):
    if not trip.can_transition_to(Trip.Status.IN_PROGRESS):
        raise ValidationError(f"Trip can't be resumed from its current status ({trip.get_status_display()}).")

    trip.status = Trip.Status.IN_PROGRESS
    trip.updated_by = resumed_by
    trip.save(update_fields=["status", "updated_by", "updated_at"])

    log_action(
        action=AuditLog.Action.RESUME, module="trip", entity="Trip", entity_id=str(trip.pk),
        new_value={"status": trip.status}, user=resumed_by, request=request,
    )
    return trip


@transaction.atomic
def complete_trip(*, trip, actual_end, actual_distance, completion_notes, fuel_used, driver_remarks, completed_by, request=None):
    if not trip.can_transition_to(Trip.Status.COMPLETED):
        raise ValidationError(f"Trip can't be completed from its current status ({trip.get_status_display()}).")

    trip.status = Trip.Status.COMPLETED
    trip.actual_end = actual_end
    trip.actual_distance = actual_distance
    trip.completion_notes = completion_notes
    trip.fuel_used = fuel_used
    trip.driver_remarks = driver_remarks
    trip.updated_by = completed_by
    trip.save(
        update_fields=[
            "status", "actual_end", "actual_distance", "completion_notes",
            "fuel_used", "driver_remarks", "updated_by", "updated_at",
        ]
    )

    _revert_vehicle_availability(trip.vehicle, completed_by)

    log_action(
        action=AuditLog.Action.COMPLETE, module="trip", entity="Trip", entity_id=str(trip.pk),
        new_value={
            "actual_end": trip.actual_end.isoformat() if trip.actual_end else None,
            "actual_distance": str(actual_distance) if actual_distance is not None else None,
        },
        user=completed_by, request=request,
    )
    return trip


@transaction.atomic
def cancel_trip(*, trip, cancellation_reason, cancelled_by, request=None):
    if not trip.can_transition_to(Trip.Status.CANCELLED):
        raise ValidationError(f"Trip can't be cancelled from its current status ({trip.get_status_display()}).")
    if not cancellation_reason or not cancellation_reason.strip():
        raise ValidationError("A cancellation reason is required.")

    was_active = trip.status in Trip.ACTIVE_ASSIGNMENT_STATUSES
    trip.status = Trip.Status.CANCELLED
    trip.cancellation_reason = cancellation_reason
    trip.updated_by = cancelled_by
    trip.save(update_fields=["status", "cancellation_reason", "updated_by", "updated_at"])

    if was_active:
        _revert_vehicle_availability(trip.vehicle, cancelled_by)

    log_action(
        action=AuditLog.Action.CANCEL, module="trip", entity="Trip", entity_id=str(trip.pk),
        new_value={"cancellation_reason": cancellation_reason},
        user=cancelled_by, request=request,
    )
    return trip
