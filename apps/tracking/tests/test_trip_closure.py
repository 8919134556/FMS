"""Per-vehicle Trip Closure Time (Vehicle.trip_closure_minutes): each
vehicle's trips are split with ITS OWN value — ignition back ON before the
closure time continues the trip, OFF for the closure time or longer closes
it — and changing one vehicle never changes another vehicle's trips."""

import datetime

import pytest
from django.utils import timezone

from apps.tracking import trip_report
from apps.tracking.tests.factories import TelemetryEventFactory, TrackingDeviceFactory
from apps.vehicles.models import DEFAULT_TRIP_CLOSURE_MINUTES
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db

M = datetime.timedelta(minutes=1)


def _base():
    return timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)


def _vehicle(minutes=None):
    vehicle = VehicleFactory() if minutes is None else VehicleFactory(trip_closure_minutes=minutes)
    TrackingDeviceFactory(vehicle=vehicle)
    return vehicle


def _event(vehicle, when, ignition, odometer):
    TelemetryEventFactory(device=vehicle.tracking_device, vehicle=vehicle, timestamp=when, ignition=ignition,
                          odometer=odometer)


def _drive_stop_drive(vehicle, base, off_minutes):
    """Drive 10 min, ignition OFF for ``off_minutes``, drive 10 min, park."""
    _event(vehicle, base, True, 100)
    _event(vehicle, base + 10 * M, False, 110)
    _event(vehicle, base + (10 + off_minutes) * M, True, 110)
    _event(vehicle, base + (20 + off_minutes) * M, False, 120)


def _trips(now, vehicle=None):
    trips, _ = trip_report.compute_vehicle_trips(
        user=None, start_date=now.date(), end_date=now.date(), now=now,
        vehicle_uuid=str(vehicle.uuid) if vehicle else "",
    )
    return trips


@pytest.mark.parametrize(
    "closure, off_minutes, expected_trips",
    [
        (5, 4, 1),    # ON again before 5 min  -> same trip
        (5, 5, 2),    # OFF exactly 5 min      -> closed, new trip
        (5, 6, 2),    # OFF past 5 min         -> new trip
        (10, 6, 1),   # the same 6-min stop is still ONE trip on a 10-min vehicle
        (10, 9, 1),
        (10, 10, 2),
        (10, 11, 2),
        (15, 14, 1),
        (15, 15, 2),
        (15, 16, 2),
        (20, 19, 1),
        (20, 20, 2),
    ],
)
def test_the_vehicles_own_closure_time_splits_its_trips(closure, off_minutes, expected_trips):
    vehicle = _vehicle(closure)
    base = _base()
    _drive_stop_drive(vehicle, base, off_minutes)

    trips = _trips(base + 3 * 60 * M)

    assert len(trips) == expected_trips
    assert all(t.status == "COMPLETED" for t in trips)
    if expected_trips == 1:
        # One continuous trip: first ON to final OFF, full odometer distance.
        assert trips[0].start_at == base and trips[0].end_at == base + (20 + off_minutes) * M
        assert trips[0].distance_km == 20
    else:
        # The first trip ended the moment ignition went OFF, not when the timeout elapsed.
        newest, oldest = trips
        assert oldest.end_at == base + 10 * M and oldest.distance_km == 10
        assert newest.start_at == base + (10 + off_minutes) * M and newest.distance_km == 10


@pytest.mark.parametrize("closure, expected_status", [(5, "COMPLETED"), (10, "ACTIVE"), (15, "ACTIVE")])
def test_an_open_stop_closes_only_once_that_vehicles_timeout_has_elapsed(closure, expected_status):
    """Ignition went OFF 8 minutes ago and hasn't come back: closed for a
    5-min vehicle, still an ACTIVE trip (may resume) for 10/15-min vehicles."""
    vehicle = _vehicle(closure)
    base = _base()
    _event(vehicle, base, True, 100)
    _event(vehicle, base + 10 * M, False, 105)

    trips = _trips(base + 18 * M)

    assert len(trips) == 1 and trips[0].status == expected_status
    if expected_status == "ACTIVE":
        assert trips[0].end_at is None and trips[0].ignition is False


def test_vehicles_with_different_closure_times_in_one_report():
    """Identical ignition history (a 12-minute stop) on four vehicles, computed
    in a single call: each is judged by its own setting."""
    base = _base()
    vehicles = {minutes: _vehicle(minutes) for minutes in (5, 10, 15, 20)}
    for vehicle in vehicles.values():
        _drive_stop_drive(vehicle, base, off_minutes=12)

    trips = _trips(base + 3 * 60 * M)

    per_vehicle = {m: sum(1 for t in trips if t.vehicle.id == v.id) for m, v in vehicles.items()}
    assert per_vehicle == {5: 2, 10: 2, 15: 1, 20: 1}


def test_changing_one_vehicles_closure_time_does_not_affect_the_others():
    base = _base()
    changed, untouched = _vehicle(10), _vehicle(10)
    for vehicle in (changed, untouched):
        _drive_stop_drive(vehicle, base, off_minutes=12)
    now = base + 3 * 60 * M

    before = {v.id: len(_trips(now, v)) for v in (changed, untouched)}
    changed.trip_closure_minutes = 15
    changed.save(update_fields=["trip_closure_minutes"])
    after = {v.id: len(_trips(now, v)) for v in (changed, untouched)}

    assert before == {changed.id: 2, untouched.id: 2}
    assert after == {changed.id: 1, untouched.id: 2}  # only the edited vehicle re-split
    untouched.refresh_from_db()
    assert untouched.trip_closure_minutes == 10


def test_existing_and_new_vehicles_default_to_five_minutes():
    vehicle = _vehicle()
    assert DEFAULT_TRIP_CLOSURE_MINUTES == 5
    assert vehicle.trip_closure_minutes == 5
    assert trip_report.trip_closure_window(vehicle) == 5 * M


@pytest.mark.parametrize("bad", [None, 0])
def test_a_missing_value_falls_back_to_the_default_instead_of_breaking(bad):
    vehicle = VehicleFactory.build()
    vehicle.trip_closure_minutes = bad
    assert trip_report.trip_closure_window(vehicle) == DEFAULT_TRIP_CLOSURE_MINUTES * M
