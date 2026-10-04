import datetime

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.maintenance import services
from apps.maintenance.models import Maintenance
from apps.maintenance.tests.factories import MaintenanceFactory
from apps.trips.models import Trip
from apps.trips.tests.factories import TripFactory
from apps.vehicles.models import Vehicle
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


class TestGenerateMaintenanceNumber:
    def test_generates_sequential_numbers(self):
        first = services.generate_maintenance_number()
        second = services.generate_maintenance_number()
        assert first != second
        assert first.startswith("MNT-")
        assert int(second.split("-")[1]) == int(first.split("-")[1]) + 1


class TestStartMaintenance:
    def test_start_success_sets_vehicle_under_maintenance(self):
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE)
        maintenance = MaintenanceFactory(vehicle=vehicle, status=Maintenance.Status.SCHEDULED)
        actor = UserFactory()
        services.start_maintenance(maintenance=maintenance, started_by=actor)
        maintenance.refresh_from_db()
        vehicle.refresh_from_db()
        assert maintenance.status == Maintenance.Status.IN_PROGRESS
        assert maintenance.started_date is not None
        assert vehicle.status == Vehicle.Status.UNDER_MAINTENANCE
        assert vehicle.availability_status == Vehicle.AvailabilityStatus.MAINTENANCE

    def test_start_wrong_status_rejected(self):
        maintenance = MaintenanceFactory(status=Maintenance.Status.COMPLETED)
        actor = UserFactory()
        with pytest.raises(ValidationError):
            services.start_maintenance(maintenance=maintenance, started_by=actor)

    def test_start_blocked_when_vehicle_has_active_trip(self):
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE)
        maintenance = MaintenanceFactory(vehicle=vehicle, status=Maintenance.Status.SCHEDULED)
        TripFactory(vehicle=vehicle, status=Trip.Status.IN_PROGRESS)
        actor = UserFactory()
        with pytest.raises(ValidationError, match="active trip"):
            services.start_maintenance(maintenance=maintenance, started_by=actor)
        maintenance.refresh_from_db()
        assert maintenance.status == Maintenance.Status.SCHEDULED

    def test_start_allowed_when_vehicle_trip_is_completed(self):
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE)
        maintenance = MaintenanceFactory(vehicle=vehicle, status=Maintenance.Status.SCHEDULED)
        TripFactory(vehicle=vehicle, status=Trip.Status.COMPLETED)
        actor = UserFactory()
        services.start_maintenance(maintenance=maintenance, started_by=actor)
        maintenance.refresh_from_db()
        assert maintenance.status == Maintenance.Status.IN_PROGRESS


class TestCompleteMaintenance:
    def test_complete_success_updates_vehicle_service_info(self):
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE, odometer_reading=9000)
        maintenance = MaintenanceFactory(vehicle=vehicle, status=Maintenance.Status.SCHEDULED)
        actor = UserFactory()
        services.start_maintenance(maintenance=maintenance, started_by=actor)

        completed_date = timezone.now()
        next_service_date = timezone.now().date() + datetime.timedelta(days=90)
        services.complete_maintenance(
            maintenance=maintenance, completed_date=completed_date, final_odometer=9500,
            actual_cost=1500, labor_cost=500, work_performed="Oil changed",
            completion_notes="All good", next_service_date=next_service_date, next_service_odometer=15000,
            completed_by=actor,
        )
        maintenance.refresh_from_db()
        vehicle.refresh_from_db()

        assert maintenance.status == Maintenance.Status.COMPLETED
        assert maintenance.actual_cost == 1500
        assert vehicle.status == Vehicle.Status.ACTIVE
        assert vehicle.availability_status in (Vehicle.AvailabilityStatus.AVAILABLE, Vehicle.AvailabilityStatus.ASSIGNED)
        assert vehicle.last_service_date == completed_date.date()
        assert vehicle.last_service_odometer == 9500
        assert vehicle.odometer_reading == 9500
        assert vehicle.next_service_date == next_service_date
        assert vehicle.next_service_odometer == 15000

    def test_complete_does_not_lower_vehicle_odometer(self):
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE, odometer_reading=20000)
        maintenance = MaintenanceFactory(vehicle=vehicle, status=Maintenance.Status.SCHEDULED)
        actor = UserFactory()
        services.start_maintenance(maintenance=maintenance, started_by=actor)
        services.complete_maintenance(
            maintenance=maintenance, completed_date=timezone.now(), final_odometer=15000,
            actual_cost=None, labor_cost=None, work_performed="", completion_notes="",
            next_service_date=None, next_service_odometer=None, completed_by=actor,
        )
        vehicle.refresh_from_db()
        assert vehicle.odometer_reading == 20000

    def test_complete_wrong_status_rejected(self):
        maintenance = MaintenanceFactory(status=Maintenance.Status.SCHEDULED)
        actor = UserFactory()
        with pytest.raises(ValidationError):
            services.complete_maintenance(
                maintenance=maintenance, completed_date=timezone.now(), final_odometer=None,
                actual_cost=None, labor_cost=None, work_performed="", completion_notes="",
                next_service_date=None, next_service_odometer=None, completed_by=actor,
            )


class TestCancelMaintenance:
    def test_cancel_requires_reason(self):
        maintenance = MaintenanceFactory(status=Maintenance.Status.SCHEDULED)
        actor = UserFactory()
        with pytest.raises(ValidationError, match="reason is required"):
            services.cancel_maintenance(maintenance=maintenance, cancellation_reason="", cancelled_by=actor)

    def test_cancel_scheduled_success(self):
        maintenance = MaintenanceFactory(status=Maintenance.Status.SCHEDULED)
        actor = UserFactory()
        services.cancel_maintenance(maintenance=maintenance, cancellation_reason="No longer needed", cancelled_by=actor)
        maintenance.refresh_from_db()
        assert maintenance.status == Maintenance.Status.CANCELLED

    def test_cancel_completed_rejected(self):
        maintenance = MaintenanceFactory(status=Maintenance.Status.COMPLETED)
        actor = UserFactory()
        with pytest.raises(ValidationError):
            services.cancel_maintenance(maintenance=maintenance, cancellation_reason="too late", cancelled_by=actor)

    def test_cancelling_in_progress_reverts_vehicle_status(self):
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE)
        maintenance = MaintenanceFactory(vehicle=vehicle, status=Maintenance.Status.SCHEDULED)
        actor = UserFactory()
        services.start_maintenance(maintenance=maintenance, started_by=actor)
        vehicle.refresh_from_db()
        assert vehicle.status == Vehicle.Status.UNDER_MAINTENANCE

        services.cancel_maintenance(maintenance=maintenance, cancellation_reason="Vehicle needed urgently", cancelled_by=actor)
        vehicle.refresh_from_db()
        assert vehicle.status == Vehicle.Status.ACTIVE


class TestTripVehicleConflictIntegration:
    def test_vehicle_under_maintenance_cannot_be_assigned_to_new_trip(self):
        from apps.drivers.models import Driver
        from apps.drivers.tests.factories import DriverFactory
        from apps.trips import services as trip_services

        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE)
        maintenance = MaintenanceFactory(vehicle=vehicle, status=Maintenance.Status.SCHEDULED)
        actor = UserFactory()
        services.start_maintenance(maintenance=maintenance, started_by=actor)

        trip = TripFactory(status=Trip.Status.SCHEDULED)
        driver = DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE)
        with pytest.raises(ValidationError, match="not active"):
            trip_services.assign_trip(trip=trip, vehicle=vehicle, driver=driver, assigned_by=actor)
