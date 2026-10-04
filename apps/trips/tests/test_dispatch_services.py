import datetime

import pytest
from django.utils import timezone

from apps.drivers.models import Driver
from apps.drivers.tests.factories import DriverFactory
from apps.maintenance.tests.factories import MaintenanceFactory
from apps.trips import dispatch_services
from apps.trips.models import Trip
from apps.trips.tests.factories import TripFactory
from apps.vehicles.models import Vehicle
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


class TestGroupIntoColumns:
    def test_buckets_by_status(self):
        scheduled = TripFactory(status=Trip.Status.SCHEDULED)
        assigned = TripFactory(status=Trip.Status.ASSIGNED)
        dispatched = TripFactory(status=Trip.Status.DISPATCHED)
        in_progress = TripFactory(status=Trip.Status.IN_PROGRESS)
        delayed = TripFactory(status=Trip.Status.DELAYED)

        columns = dispatch_services.group_into_columns(Trip.objects.all())
        by_key = {c["key"]: c for c in columns}

        assert [t.pk for t in by_key["needs_assignment"]["trips"]] == [scheduled.pk]
        assert [t.pk for t in by_key["ready"]["trips"]] == [assigned.pk]
        assert [t.pk for t in by_key["dispatched"]["trips"]] == [dispatched.pk]
        assert [t.pk for t in by_key["in_progress"]["trips"]] == [in_progress.pk]
        assert [t.pk for t in by_key["delayed"]["trips"]] == [delayed.pk]
        for c in columns:
            assert c["count"] == 1

    def test_draft_completed_cancelled_never_appear(self):
        TripFactory(status=Trip.Status.DRAFT)
        TripFactory(status=Trip.Status.COMPLETED)
        TripFactory(status=Trip.Status.CANCELLED)

        columns = dispatch_services.group_into_columns(Trip.objects.all())

        assert sum(c["count"] for c in columns) == 0

    def test_empty_day(self):
        columns = dispatch_services.group_into_columns(Trip.objects.none())
        assert all(c["count"] == 0 for c in columns)
        assert all(c["trips"] == [] for c in columns)


class TestAvailableVehicles:
    def test_active_and_available_included(self):
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE, availability_status=Vehicle.AvailabilityStatus.AVAILABLE)
        result_ids = {v.id for v in dispatch_services.available_vehicles()}
        assert vehicle.id in result_ids

    def test_inactive_status_excluded(self):
        VehicleFactory(status=Vehicle.Status.INACTIVE, availability_status=Vehicle.AvailabilityStatus.AVAILABLE)
        assert list(dispatch_services.available_vehicles()) == []

    def test_on_trip_availability_excluded(self):
        VehicleFactory(status=Vehicle.Status.ACTIVE, availability_status=Vehicle.AvailabilityStatus.ON_TRIP)
        assert list(dispatch_services.available_vehicles()) == []

    def test_next_assignment_populated(self):
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE, availability_status=Vehicle.AvailabilityStatus.AVAILABLE)
        trip = TripFactory(
            status=Trip.Status.ASSIGNED,
            vehicle=vehicle,
            scheduled_start=timezone.now() + datetime.timedelta(hours=2),
            scheduled_end=timezone.now() + datetime.timedelta(hours=4),
        )
        result = {v.id: v for v in dispatch_services.available_vehicles()}
        assert result[vehicle.id].next_assignment == trip.trip_number

    def test_next_assignment_none_without_future_trip(self):
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE, availability_status=Vehicle.AvailabilityStatus.AVAILABLE)
        result = {v.id: v for v in dispatch_services.available_vehicles()}
        assert result[vehicle.id].next_assignment is None


class TestAvailableDrivers:
    def test_active_employment_included(self):
        driver = DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE)
        result_ids = {d.id for d in dispatch_services.available_drivers()}
        assert driver.id in result_ids

    def test_inactive_employment_excluded(self):
        DriverFactory(employment_status=Driver.EmploymentStatus.INACTIVE)
        assert list(dispatch_services.available_drivers()) == []

    def test_expired_license_excluded(self):
        DriverFactory(
            employment_status=Driver.EmploymentStatus.ACTIVE,
            license_expiry_date=timezone.now().date() - datetime.timedelta(days=1),
        )
        assert list(dispatch_services.available_drivers()) == []

    def test_no_license_expiry_date_included(self):
        driver = DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE, license_expiry_date=None)
        result_ids = {d.id for d in dispatch_services.available_drivers()}
        assert driver.id in result_ids

    def test_next_assignment_populated(self):
        driver = DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE)
        trip = TripFactory(
            status=Trip.Status.ASSIGNED,
            driver=driver,
            scheduled_start=timezone.now() + datetime.timedelta(hours=2),
            scheduled_end=timezone.now() + datetime.timedelta(hours=4),
        )
        result = {d.id: d for d in dispatch_services.available_drivers()}
        assert result[driver.id].next_assignment == trip.trip_number


class TestDispatchAlerts:
    def test_unassigned_starting_soon(self):
        TripFactory(
            status=Trip.Status.SCHEDULED,
            scheduled_start=timezone.now() + datetime.timedelta(hours=1),
            scheduled_end=timezone.now() + datetime.timedelta(hours=3),
        )
        alerts = dispatch_services.dispatch_alerts(_superuser())
        assert any("starts soon" in a["title"] for a in alerts)

    def test_starting_soon_boundary_excludes_beyond_window(self):
        TripFactory(
            status=Trip.Status.SCHEDULED,
            scheduled_start=timezone.now() + datetime.timedelta(hours=dispatch_services.STARTING_SOON_WINDOW_HOURS, minutes=5),
            scheduled_end=timezone.now() + datetime.timedelta(hours=5),
        )
        alerts = dispatch_services.dispatch_alerts(_superuser())
        assert not any("starts soon" in a["title"] for a in alerts)

    def test_vehicle_without_driver(self):
        vehicle = VehicleFactory()
        TripFactory(status=Trip.Status.ASSIGNED, vehicle=vehicle, driver=None)
        alerts = dispatch_services.dispatch_alerts(_superuser())
        assert any("missing a driver" in a["title"] for a in alerts)

    def test_driver_without_vehicle(self):
        driver = DriverFactory()
        TripFactory(status=Trip.Status.ASSIGNED, driver=driver, vehicle=None)
        alerts = dispatch_services.dispatch_alerts(_superuser())
        assert any("missing a vehicle" in a["title"] for a in alerts)

    def test_delayed_trip_alert(self):
        TripFactory(status=Trip.Status.DELAYED)
        alerts = dispatch_services.dispatch_alerts(_superuser())
        assert any("delayed trip" in a["title"].lower() for a in alerts)

    def test_past_due_alert_never_mutates_trip(self):
        trip = TripFactory(
            status=Trip.Status.SCHEDULED,
            scheduled_start=timezone.now() - datetime.timedelta(hours=1),
            scheduled_end=timezone.now() + datetime.timedelta(hours=1),
        )
        alerts = dispatch_services.dispatch_alerts(_superuser())
        assert any("past due" in a["title"] for a in alerts)
        trip.refresh_from_db()
        assert trip.status == Trip.Status.SCHEDULED

    def test_vehicle_conflict_alert(self):
        vehicle = VehicleFactory()
        now = timezone.now()
        TripFactory(
            status=Trip.Status.ASSIGNED, vehicle=vehicle,
            scheduled_start=now + datetime.timedelta(hours=1), scheduled_end=now + datetime.timedelta(hours=3),
        )
        TripFactory(
            status=Trip.Status.ASSIGNED, vehicle=vehicle,
            scheduled_start=now + datetime.timedelta(hours=2), scheduled_end=now + datetime.timedelta(hours=4),
        )
        alerts = dispatch_services.dispatch_alerts(_superuser())
        matching = [a for a in alerts if "Vehicle conflict" in a["title"]]
        assert matching and matching[0]["category"] == "conflict"

    def test_driver_conflict_alert(self):
        driver = DriverFactory()
        now = timezone.now()
        TripFactory(
            status=Trip.Status.ASSIGNED, driver=driver,
            scheduled_start=now + datetime.timedelta(hours=1), scheduled_end=now + datetime.timedelta(hours=3),
        )
        TripFactory(
            status=Trip.Status.ASSIGNED, driver=driver,
            scheduled_start=now + datetime.timedelta(hours=2), scheduled_end=now + datetime.timedelta(hours=4),
        )
        alerts = dispatch_services.dispatch_alerts(_superuser())
        assert any("Driver conflict" in a["title"] for a in alerts)

    def test_maintenance_conflict_alert(self):
        vehicle = VehicleFactory(status=Vehicle.Status.UNDER_MAINTENANCE)
        TripFactory(status=Trip.Status.ASSIGNED, vehicle=vehicle)
        alerts = dispatch_services.dispatch_alerts(_superuser())
        assert any("maintenance conflict" in a["title"] for a in alerts)

    def test_expired_license_alert(self):
        driver = DriverFactory(license_expiry_date=timezone.now().date() - datetime.timedelta(days=1))
        TripFactory(status=Trip.Status.ASSIGNED, driver=driver)
        alerts = dispatch_services.dispatch_alerts(_superuser())
        assert any("license" in a["title"] and "expired" in a["title"] for a in alerts)

    def test_no_permission_returns_empty(self):
        from apps.accounts.tests.factories import UserFactory

        viewer = UserFactory(role=None)
        assert dispatch_services.dispatch_alerts(viewer) == []

    def test_unassigned_starting_soon_is_attention_not_conflict(self):
        TripFactory(status=Trip.Status.SCHEDULED, scheduled_start=timezone.now() + datetime.timedelta(minutes=30))
        alerts = dispatch_services.dispatch_alerts(_superuser())
        matching = [a for a in alerts if "starts soon" in a["title"]]
        assert matching and matching[0]["category"] == "attention"


class TestQuickFilterCounts:
    def test_counts_every_bucket(self):
        today = timezone.now().date()
        TripFactory(status=Trip.Status.SCHEDULED, scheduled_start=timezone.now())
        TripFactory(status=Trip.Status.DELAYED, scheduled_start=timezone.now())
        counts = dispatch_services.quick_filter_counts(today, {})
        assert counts["needs_assignment"] == 1
        assert counts["delayed"] == 1
        assert counts["ready"] == 0
        assert counts["all"] == 2

    def test_scoped_by_other_filters(self):
        from apps.clients.tests.factories import ClientFactory

        today = timezone.now().date()
        target_client = ClientFactory()
        TripFactory(status=Trip.Status.SCHEDULED, client=target_client, scheduled_start=timezone.now())
        TripFactory(status=Trip.Status.SCHEDULED, scheduled_start=timezone.now())
        counts = dispatch_services.quick_filter_counts(today, {"client": str(target_client.id)})
        assert counts["needs_assignment"] == 1
        assert counts["all"] == 1


def _superuser():
    from apps.accounts.tests.factories import UserFactory

    return UserFactory(is_superuser=True)
