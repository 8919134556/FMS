import datetime

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.clients.tests.factories import ClientFactory
from apps.drivers.models import Driver
from apps.drivers.tests.factories import DriverFactory
from apps.trips import services
from apps.trips.models import Trip
from apps.trips.tests.factories import TripFactory
from apps.vehicles.models import Vehicle
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


def _assign_ready_trip(**kwargs):
    kwargs.setdefault("status", Trip.Status.SCHEDULED)
    trip = TripFactory(**kwargs)
    vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE, client=trip.client)
    driver = DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE, client=trip.client)
    return trip, vehicle, driver


class TestAssignTrip:
    def test_assign_success_transitions_to_assigned(self):
        trip, vehicle, driver = _assign_ready_trip()
        actor = UserFactory()
        services.assign_trip(trip=trip, vehicle=vehicle, driver=driver, assigned_by=actor)
        trip.refresh_from_db()
        assert trip.status == Trip.Status.ASSIGNED
        assert trip.vehicle_id == vehicle.id
        assert trip.driver_id == driver.id

    def test_assign_wrong_status_rejected(self):
        trip, vehicle, driver = _assign_ready_trip(status=Trip.Status.DRAFT)
        actor = UserFactory()
        with pytest.raises(ValidationError):
            services.assign_trip(trip=trip, vehicle=vehicle, driver=driver, assigned_by=actor)

    def test_assign_inactive_vehicle_rejected(self):
        trip, vehicle, driver = _assign_ready_trip()
        vehicle.status = Vehicle.Status.INACTIVE
        vehicle.save()
        actor = UserFactory()
        with pytest.raises(ValidationError, match="not active"):
            services.assign_trip(trip=trip, vehicle=vehicle, driver=driver, assigned_by=actor)

    def test_assign_inactive_driver_rejected(self):
        trip, vehicle, driver = _assign_ready_trip()
        driver.employment_status = Driver.EmploymentStatus.INACTIVE
        driver.save()
        actor = UserFactory()
        with pytest.raises(ValidationError, match="not an active driver"):
            services.assign_trip(trip=trip, vehicle=vehicle, driver=driver, assigned_by=actor)

    def test_assign_expired_license_driver_rejected(self):
        trip, vehicle, driver = _assign_ready_trip()
        driver.license_expiry_date = timezone.now().date() - datetime.timedelta(days=5)
        driver.save()
        actor = UserFactory()
        with pytest.raises(ValidationError, match="license has expired"):
            services.assign_trip(trip=trip, vehicle=vehicle, driver=driver, assigned_by=actor)

    def test_assign_vehicle_from_different_client_rejected(self):
        trip, vehicle, driver = _assign_ready_trip()
        other_client_vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE, client=ClientFactory())
        actor = UserFactory()
        with pytest.raises(ValidationError, match="different client"):
            services.assign_trip(trip=trip, vehicle=other_client_vehicle, driver=driver, assigned_by=actor)

    def test_assign_driver_from_different_client_rejected(self):
        trip, vehicle, driver = _assign_ready_trip()
        other_client_driver = DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE, client=ClientFactory())
        actor = UserFactory()
        with pytest.raises(ValidationError, match="different client"):
            services.assign_trip(trip=trip, vehicle=vehicle, driver=other_client_driver, assigned_by=actor)

    def test_unallocated_vehicle_and_driver_assignable_to_any_client(self):
        trip = TripFactory(status=Trip.Status.SCHEDULED)
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE, client=None)
        driver = DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE, client=None)
        actor = UserFactory()
        services.assign_trip(trip=trip, vehicle=vehicle, driver=driver, assigned_by=actor)
        trip.refresh_from_db()
        assert trip.status == Trip.Status.ASSIGNED


class TestOverlapConflictDetection:
    def test_vehicle_overlap_blocks_assignment(self):
        start = timezone.now() + datetime.timedelta(hours=2)
        end = start + datetime.timedelta(hours=4)
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE)
        existing_trip = TripFactory(
            status=Trip.Status.ASSIGNED, vehicle=vehicle,
            scheduled_start=start, scheduled_end=end,
        )
        overlapping_trip = TripFactory(
            status=Trip.Status.SCHEDULED,
            scheduled_start=start + datetime.timedelta(hours=1),
            scheduled_end=end + datetime.timedelta(hours=1),
        )
        driver = DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE)
        actor = UserFactory()
        with pytest.raises(ValidationError, match="already assigned"):
            services.assign_trip(trip=overlapping_trip, vehicle=vehicle, driver=driver, assigned_by=actor)
        assert existing_trip.trip_number  # sanity: existing trip untouched

    def test_driver_overlap_blocks_assignment(self):
        start = timezone.now() + datetime.timedelta(hours=2)
        end = start + datetime.timedelta(hours=4)
        driver = DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE)
        TripFactory(status=Trip.Status.ASSIGNED, driver=driver, scheduled_start=start, scheduled_end=end)
        overlapping_trip = TripFactory(
            status=Trip.Status.SCHEDULED,
            scheduled_start=start + datetime.timedelta(hours=1),
            scheduled_end=end + datetime.timedelta(hours=1),
        )
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE)
        actor = UserFactory()
        with pytest.raises(ValidationError, match="already assigned"):
            services.assign_trip(trip=overlapping_trip, vehicle=vehicle, driver=driver, assigned_by=actor)

    def test_non_overlapping_times_allowed(self):
        start = timezone.now() + datetime.timedelta(hours=2)
        end = start + datetime.timedelta(hours=2)
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE)
        driver = DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE)
        TripFactory(status=Trip.Status.ASSIGNED, vehicle=vehicle, driver=driver, scheduled_start=start, scheduled_end=end)
        later_trip = TripFactory(
            status=Trip.Status.SCHEDULED,
            scheduled_start=end + datetime.timedelta(hours=1),
            scheduled_end=end + datetime.timedelta(hours=3),
        )
        actor = UserFactory()
        services.assign_trip(trip=later_trip, vehicle=vehicle, driver=driver, assigned_by=actor)
        later_trip.refresh_from_db()
        assert later_trip.status == Trip.Status.ASSIGNED

    def test_completed_trip_does_not_conflict(self):
        start = timezone.now() + datetime.timedelta(hours=2)
        end = start + datetime.timedelta(hours=4)
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE)
        TripFactory(status=Trip.Status.COMPLETED, vehicle=vehicle, scheduled_start=start, scheduled_end=end)
        new_trip = TripFactory(status=Trip.Status.SCHEDULED, scheduled_start=start, scheduled_end=end)
        driver = DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE)
        actor = UserFactory()
        services.assign_trip(trip=new_trip, vehicle=vehicle, driver=driver, assigned_by=actor)
        new_trip.refresh_from_db()
        assert new_trip.status == Trip.Status.ASSIGNED


class TestTripLifecycle:
    def test_full_lifecycle_dispatch_start_complete(self):
        trip, vehicle, driver = _assign_ready_trip()
        actor = UserFactory()
        services.assign_trip(trip=trip, vehicle=vehicle, driver=driver, assigned_by=actor)

        services.dispatch_trip(trip=trip, dispatched_by=actor)
        trip.refresh_from_db()
        vehicle.refresh_from_db()
        assert trip.status == Trip.Status.DISPATCHED
        assert vehicle.availability_status == Vehicle.AvailabilityStatus.ON_TRIP

        services.start_trip(trip=trip, started_by=actor)
        trip.refresh_from_db()
        assert trip.status == Trip.Status.IN_PROGRESS
        assert trip.actual_start is not None

        services.complete_trip(
            trip=trip, actual_end=timezone.now(), actual_distance=120, completion_notes="done",
            fuel_used=15, driver_remarks="smooth trip", completed_by=actor,
        )
        trip.refresh_from_db()
        vehicle.refresh_from_db()
        assert trip.status == Trip.Status.COMPLETED
        assert trip.actual_distance == 120
        assert vehicle.availability_status in (Vehicle.AvailabilityStatus.AVAILABLE, Vehicle.AvailabilityStatus.ASSIGNED)

    def test_delay_then_resume_then_complete(self):
        trip, vehicle, driver = _assign_ready_trip()
        actor = UserFactory()
        services.assign_trip(trip=trip, vehicle=vehicle, driver=driver, assigned_by=actor)
        services.dispatch_trip(trip=trip, dispatched_by=actor)
        services.start_trip(trip=trip, started_by=actor)

        services.delay_trip(trip=trip, delay_reason=Trip.DelayReason.TRAFFIC, delay_notes="Heavy traffic", delayed_by=actor)
        trip.refresh_from_db()
        assert trip.status == Trip.Status.DELAYED
        assert trip.delay_reason == Trip.DelayReason.TRAFFIC

        services.resume_trip(trip=trip, resumed_by=actor)
        trip.refresh_from_db()
        assert trip.status == Trip.Status.IN_PROGRESS

        services.complete_trip(
            trip=trip, actual_end=timezone.now(), actual_distance=80, completion_notes="",
            fuel_used=None, driver_remarks="", completed_by=actor,
        )
        trip.refresh_from_db()
        assert trip.status == Trip.Status.COMPLETED

    def test_dispatch_wrong_status_rejected(self):
        trip = TripFactory(status=Trip.Status.SCHEDULED)
        actor = UserFactory()
        with pytest.raises(ValidationError):
            services.dispatch_trip(trip=trip, dispatched_by=actor)

    def test_complete_from_in_progress_or_delayed_only(self):
        trip = TripFactory(status=Trip.Status.SCHEDULED)
        actor = UserFactory()
        with pytest.raises(ValidationError):
            services.complete_trip(
                trip=trip, actual_end=timezone.now(), actual_distance=None,
                completion_notes="", fuel_used=None, driver_remarks="", completed_by=actor,
            )


class TestCancelTrip:
    def test_cancel_requires_reason(self):
        trip = TripFactory(status=Trip.Status.SCHEDULED)
        actor = UserFactory()
        with pytest.raises(ValidationError, match="reason is required"):
            services.cancel_trip(trip=trip, cancellation_reason="", cancelled_by=actor)

    def test_cancel_success(self):
        trip = TripFactory(status=Trip.Status.SCHEDULED)
        actor = UserFactory()
        services.cancel_trip(trip=trip, cancellation_reason="Client cancelled order", cancelled_by=actor)
        trip.refresh_from_db()
        assert trip.status == Trip.Status.CANCELLED
        assert trip.cancellation_reason == "Client cancelled order"

    def test_cancel_completed_trip_rejected(self):
        trip = TripFactory(status=Trip.Status.COMPLETED)
        actor = UserFactory()
        with pytest.raises(ValidationError):
            services.cancel_trip(trip=trip, cancellation_reason="too late", cancelled_by=actor)

    def test_cancelling_dispatched_trip_reverts_vehicle_availability(self):
        trip, vehicle, driver = _assign_ready_trip()
        actor = UserFactory()
        services.assign_trip(trip=trip, vehicle=vehicle, driver=driver, assigned_by=actor)
        services.dispatch_trip(trip=trip, dispatched_by=actor)
        vehicle.refresh_from_db()
        assert vehicle.availability_status == Vehicle.AvailabilityStatus.ON_TRIP

        services.cancel_trip(trip=trip, cancellation_reason="Vehicle breakdown", cancelled_by=actor)
        vehicle.refresh_from_db()
        assert vehicle.availability_status in (Vehicle.AvailabilityStatus.AVAILABLE, Vehicle.AvailabilityStatus.ASSIGNED)


class TestScheduleTrip:
    def test_schedule_draft_trip(self):
        trip = TripFactory(status=Trip.Status.DRAFT)
        actor = UserFactory()
        services.schedule_trip(trip=trip, scheduled_by=actor)
        trip.refresh_from_db()
        assert trip.status == Trip.Status.SCHEDULED

    def test_schedule_already_scheduled_trip_rejected(self):
        trip = TripFactory(status=Trip.Status.SCHEDULED)
        actor = UserFactory()
        with pytest.raises(ValidationError):
            services.schedule_trip(trip=trip, scheduled_by=actor)


class TestTripStatusCounts:
    def test_counts_every_status_including_zero(self):
        TripFactory(status=Trip.Status.SCHEDULED)
        TripFactory(status=Trip.Status.SCHEDULED)
        TripFactory(status=Trip.Status.CANCELLED)

        counts = services.trip_status_counts(Trip.objects.all())
        assert counts[Trip.Status.SCHEDULED] == 2
        assert counts[Trip.Status.CANCELLED] == 1
        assert counts[Trip.Status.DRAFT] == 0  # present with 0, never omitted
        assert set(counts.keys()) == {choice for choice, _ in Trip.Status.choices}

    def test_respects_a_pre_filtered_queryset(self):
        client_obj = ClientFactory()
        TripFactory(client=client_obj, status=Trip.Status.DELAYED)
        TripFactory(status=Trip.Status.DELAYED)  # different client

        counts = services.trip_status_counts(Trip.objects.filter(client=client_obj))
        assert counts[Trip.Status.DELAYED] == 1
