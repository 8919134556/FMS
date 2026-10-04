import datetime

import pytest
from django.db import IntegrityError, transaction

from apps.drivers.tests.factories import DriverFactory
from apps.vehicles.models import VehicleDriverAssignment
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


class TestVehicleUniqueness:
    def test_duplicate_registration_number_rejected(self):
        VehicleFactory(registration_number="KA01ZZ0001")
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                VehicleFactory(registration_number="KA01ZZ0001")

    def test_duplicate_vin_rejected(self):
        VehicleFactory(vin="VIN12345")
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                VehicleFactory(vin="VIN12345")

    def test_null_vin_allowed_on_multiple_vehicles(self):
        VehicleFactory(vin=None)
        VehicleFactory(vin=None)  # should not raise


class TestVehicleDriverAssignmentConstraint:
    def test_two_active_primary_assignments_for_same_vehicle_rejected(self):
        vehicle = VehicleFactory()
        driver1 = DriverFactory()
        driver2 = DriverFactory()
        VehicleDriverAssignment.objects.create(
            vehicle=vehicle, driver=driver1, start_date=datetime.date.today(),
            primary_driver=True, status=VehicleDriverAssignment.Status.ACTIVE,
        )
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                VehicleDriverAssignment.objects.create(
                    vehicle=vehicle, driver=driver2, start_date=datetime.date.today(),
                    primary_driver=True, status=VehicleDriverAssignment.Status.ACTIVE,
                )

    def test_ended_assignment_does_not_block_new_active_primary(self):
        vehicle = VehicleFactory()
        driver1 = DriverFactory()
        driver2 = DriverFactory()
        VehicleDriverAssignment.objects.create(
            vehicle=vehicle, driver=driver1, start_date=datetime.date.today(),
            primary_driver=True, status=VehicleDriverAssignment.Status.ENDED,
        )
        # should not raise — the first assignment is ENDED, not ACTIVE
        VehicleDriverAssignment.objects.create(
            vehicle=vehicle, driver=driver2, start_date=datetime.date.today(),
            primary_driver=True, status=VehicleDriverAssignment.Status.ACTIVE,
        )

    def test_secondary_assignment_does_not_conflict_with_primary(self):
        vehicle = VehicleFactory()
        driver1 = DriverFactory()
        driver2 = DriverFactory()
        VehicleDriverAssignment.objects.create(
            vehicle=vehicle, driver=driver1, start_date=datetime.date.today(),
            primary_driver=True, status=VehicleDriverAssignment.Status.ACTIVE,
        )
        # should not raise — primary_driver=False is outside the constraint's condition
        VehicleDriverAssignment.objects.create(
            vehicle=vehicle, driver=driver2, start_date=datetime.date.today(),
            primary_driver=False, status=VehicleDriverAssignment.Status.ACTIVE,
        )
