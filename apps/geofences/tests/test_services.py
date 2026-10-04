import pytest
from django.utils import timezone

from apps.geofences import services
from apps.geofences.models import Geofence, GeofenceEvent
from apps.geofences.tests.factories import GeofenceFactory
from apps.notifications.models import Notification
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db

INSIDE_LAT, INSIDE_LON = "12.971600", "77.594600"
FAR_LAT, FAR_LON = "13.100000", "77.700000"  # well outside a 500m radius


class TestDistanceCalculation:
    def test_same_point_is_zero_distance(self):
        assert services._distance_meters(12.9716, 77.5946, 12.9716, 77.5946) == pytest.approx(0, abs=1)

    def test_known_distance_is_approximately_correct(self):
        # Roughly 1 degree of latitude is about 111km.
        distance = services._distance_meters(0, 0, 1, 0)
        assert distance == pytest.approx(111_195, rel=0.01)


class TestEvaluatePosition:
    def test_entering_geofence_creates_enter_event(self):
        geofence = GeofenceFactory(center_latitude=INSIDE_LAT, center_longitude=INSIDE_LON, radius_meters=500)
        vehicle = VehicleFactory()
        events = services.evaluate_position(vehicle, INSIDE_LAT, INSIDE_LON)
        assert len(events) == 1
        assert events[0].event_type == GeofenceEvent.EventType.ENTER

    def test_staying_inside_does_not_create_duplicate_enter_events(self):
        geofence = GeofenceFactory(center_latitude=INSIDE_LAT, center_longitude=INSIDE_LON, radius_meters=500)
        vehicle = VehicleFactory()
        services.evaluate_position(vehicle, INSIDE_LAT, INSIDE_LON)
        events = services.evaluate_position(vehicle, INSIDE_LAT, INSIDE_LON)
        assert len(events) == 0
        assert GeofenceEvent.objects.filter(geofence=geofence, vehicle=vehicle).count() == 1

    def test_leaving_geofence_creates_exit_event(self):
        geofence = GeofenceFactory(center_latitude=INSIDE_LAT, center_longitude=INSIDE_LON, radius_meters=500)
        vehicle = VehicleFactory()
        services.evaluate_position(vehicle, INSIDE_LAT, INSIDE_LON)
        events = services.evaluate_position(vehicle, FAR_LAT, FAR_LON)
        assert len(events) == 1
        assert events[0].event_type == GeofenceEvent.EventType.EXIT

    def test_never_entering_produces_no_events(self):
        GeofenceFactory(center_latitude=INSIDE_LAT, center_longitude=INSIDE_LON, radius_meters=500)
        vehicle = VehicleFactory()
        events = services.evaluate_position(vehicle, FAR_LAT, FAR_LON)
        assert events == []

    def test_inactive_geofence_is_ignored(self):
        GeofenceFactory(center_latitude=INSIDE_LAT, center_longitude=INSIDE_LON, radius_meters=500, status=Geofence.Status.INACTIVE)
        vehicle = VehicleFactory()
        events = services.evaluate_position(vehicle, INSIDE_LAT, INSIDE_LON)
        assert events == []

    def test_enter_notifies_client_account_manager_when_configured(self):
        from apps.clients.tests.factories import ClientFactory
        from apps.accounts.tests.factories import UserFactory

        account_manager = UserFactory(role=None)
        client_obj = ClientFactory(account_manager=account_manager)
        geofence = GeofenceFactory(center_latitude=INSIDE_LAT, center_longitude=INSIDE_LON, radius_meters=500, notify_on_enter=True)
        vehicle = VehicleFactory(client=client_obj)
        services.evaluate_position(vehicle, INSIDE_LAT, INSIDE_LON)
        assert Notification.objects.filter(recipient=account_manager).exists()

    def test_no_notification_when_notify_on_enter_is_disabled(self):
        from apps.clients.tests.factories import ClientFactory
        from apps.accounts.tests.factories import UserFactory

        account_manager = UserFactory(role=None)
        client_obj = ClientFactory(account_manager=account_manager)
        GeofenceFactory(center_latitude=INSIDE_LAT, center_longitude=INSIDE_LON, radius_meters=500, notify_on_enter=False)
        vehicle = VehicleFactory(client=client_obj)
        services.evaluate_position(vehicle, INSIDE_LAT, INSIDE_LON)
        assert not Notification.objects.filter(recipient=account_manager).exists()
