import datetime

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.drivers.models import Driver
from apps.drivers.tests.factories import DriverFactory
from apps.vehicles import services
from apps.vehicles.models import Vehicle, VehicleDriverAssignment
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


class TestAssignDriver:
    def test_assign_primary_driver_updates_vehicle(self):
        vehicle = VehicleFactory(availability_status=Vehicle.AvailabilityStatus.AVAILABLE)
        driver = DriverFactory()
        actor = UserFactory()

        assignment = services.assign_driver(
            vehicle=vehicle, driver=driver, start_date=datetime.date.today(),
            assignment_type=VehicleDriverAssignment.AssignmentType.PRIMARY,
            primary_driver=True, remarks="", assigned_by=actor,
        )

        vehicle.refresh_from_db()
        assert assignment.status == VehicleDriverAssignment.Status.ACTIVE
        assert vehicle.current_driver_id == driver.id
        assert vehicle.availability_status == Vehicle.AvailabilityStatus.ASSIGNED

    def test_reassigning_primary_driver_ends_previous_assignment(self):
        vehicle = VehicleFactory()
        driver1 = DriverFactory()
        driver2 = DriverFactory()
        actor = UserFactory()

        first = services.assign_driver(
            vehicle=vehicle, driver=driver1, start_date=datetime.date.today(),
            assignment_type=VehicleDriverAssignment.AssignmentType.PRIMARY,
            primary_driver=True, remarks="", assigned_by=actor,
        )
        second = services.assign_driver(
            vehicle=vehicle, driver=driver2, start_date=datetime.date.today(),
            assignment_type=VehicleDriverAssignment.AssignmentType.PRIMARY,
            primary_driver=True, remarks="", assigned_by=actor,
        )

        first.refresh_from_db()
        vehicle.refresh_from_db()
        assert first.status == VehicleDriverAssignment.Status.ENDED
        assert second.status == VehicleDriverAssignment.Status.ACTIVE
        assert vehicle.current_driver_id == driver2.id
        # no IntegrityError from the partial unique constraint — only one ACTIVE primary at a time
        assert VehicleDriverAssignment.objects.filter(
            vehicle=vehicle, status=VehicleDriverAssignment.Status.ACTIVE, primary_driver=True
        ).count() == 1


class TestEndAssignment:
    def test_end_assignment_clears_current_driver(self):
        vehicle = VehicleFactory()
        driver = DriverFactory()
        actor = UserFactory()

        assignment = services.assign_driver(
            vehicle=vehicle, driver=driver, start_date=datetime.date.today(),
            assignment_type=VehicleDriverAssignment.AssignmentType.PRIMARY,
            primary_driver=True, remarks="", assigned_by=actor,
        )
        services.end_assignment(assignment=assignment, ended_by=actor)

        assignment.refresh_from_db()
        vehicle.refresh_from_db()
        assert assignment.status == VehicleDriverAssignment.Status.ENDED
        assert vehicle.current_driver_id is None
        assert vehicle.availability_status == Vehicle.AvailabilityStatus.AVAILABLE
