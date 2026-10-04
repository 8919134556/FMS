import factory
from factory.django import DjangoModelFactory

from apps.vehicles.models import FuelType, Vehicle, VehicleCategory, VehicleType


class VehicleCategoryFactory(DjangoModelFactory):
    class Meta:
        model = VehicleCategory
        django_get_or_create = ("code",)

    code = factory.Sequence(lambda n: f"CAT{n}")
    name = factory.Sequence(lambda n: f"Category {n}")


class VehicleTypeFactory(DjangoModelFactory):
    class Meta:
        model = VehicleType
        django_get_or_create = ("code",)

    code = factory.Sequence(lambda n: f"VT{n}")
    name = factory.Sequence(lambda n: f"Type {n}")


class FuelTypeFactory(DjangoModelFactory):
    class Meta:
        model = FuelType
        django_get_or_create = ("code",)

    code = factory.Sequence(lambda n: f"FUEL{n}")
    name = factory.Sequence(lambda n: f"Fuel {n}")


class VehicleFactory(DjangoModelFactory):
    class Meta:
        model = Vehicle

    registration_number = factory.Sequence(lambda n: f"KA01AB{n:04d}")
    vehicle_code = factory.Sequence(lambda n: f"VEH{n:04d}")
    vehicle_type = factory.SubFactory(VehicleTypeFactory)
    fuel_type = factory.SubFactory(FuelTypeFactory)
    make = "Tata"
    model = "Ace"
    status = Vehicle.Status.ACTIVE
    availability_status = Vehicle.AvailabilityStatus.AVAILABLE
