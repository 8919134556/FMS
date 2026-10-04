import factory
from factory.django import DjangoModelFactory

from apps.drivers.models import Driver


class DriverFactory(DjangoModelFactory):
    class Meta:
        model = Driver

    employee_id = factory.Sequence(lambda n: f"DRV{n:05d}")
    first_name = "Test"
    last_name = "Driver"
    mobile_number = factory.Sequence(lambda n: f"+155501{n:05d}")
    license_number = factory.Sequence(lambda n: f"LIC{n:06d}")
    employment_status = Driver.EmploymentStatus.ACTIVE
