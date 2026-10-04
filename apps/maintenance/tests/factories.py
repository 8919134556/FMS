import datetime

import factory
from django.utils import timezone
from factory.django import DjangoModelFactory

from apps.maintenance.models import Maintenance, MaintenancePart
from apps.vehicles.tests.factories import VehicleFactory


class MaintenanceFactory(DjangoModelFactory):
    class Meta:
        model = Maintenance

    maintenance_number = factory.Sequence(lambda n: f"MNT-{n:06d}")
    vehicle = factory.SubFactory(VehicleFactory)
    maintenance_type = Maintenance.MaintenanceType.PREVENTIVE
    status = Maintenance.Status.SCHEDULED
    priority = Maintenance.Priority.MEDIUM
    scheduled_date = factory.LazyFunction(lambda: timezone.now().date() + datetime.timedelta(days=3))
    odometer_at_service = 10000


class MaintenancePartFactory(DjangoModelFactory):
    class Meta:
        model = MaintenancePart

    maintenance = factory.SubFactory(MaintenanceFactory)
    part_name = factory.Sequence(lambda n: f"Part {n}")
    quantity = 1
    unit_cost = 100
