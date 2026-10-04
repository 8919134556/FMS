import factory
from django.utils import timezone
from factory.django import DjangoModelFactory

from apps.geofences.models import Geofence, GeofenceEvent
from apps.vehicles.tests.factories import VehicleFactory


class GeofenceFactory(DjangoModelFactory):
    class Meta:
        model = Geofence

    code = factory.Sequence(lambda n: f"GEO{n:05d}")
    name = factory.Sequence(lambda n: f"Geofence {n}")
    center_latitude = "12.971600"
    center_longitude = "77.594600"
    radius_meters = 500
    status = Geofence.Status.ACTIVE


class GeofenceEventFactory(DjangoModelFactory):
    class Meta:
        model = GeofenceEvent

    geofence = factory.SubFactory(GeofenceFactory)
    vehicle = factory.SubFactory(VehicleFactory)
    event_type = GeofenceEvent.EventType.ENTER
    latitude = "12.971600"
    longitude = "77.594600"
    occurred_at = factory.LazyFunction(timezone.now)
