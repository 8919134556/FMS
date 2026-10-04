import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Permission, RolePermission
from apps.accounts.tests.factories import RoleFactory, UserFactory
from apps.tracking.tests.factories import TelemetryEventFactory, VehicleCurrentTelemetryFactory
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


def _role_with(*module_actions):
    role = RoleFactory()
    for module, action in module_actions:
        permission, _ = Permission.objects.get_or_create(module=module, action=action)
        RolePermission.objects.create(role=role, permission=permission)
    return role


def _view_role():
    return _role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW))


class TestVehicleCurrentTelemetryAPI:
    def test_requires_authentication(self, client):
        vehicle = VehicleFactory()
        response = client.get(reverse("vehicle-telemetry-current", kwargs={"vehicle_uuid": vehicle.uuid}))
        assert response.status_code in (401, 403)

    def test_requires_tracking_device_view_permission(self, client):
        vehicle = VehicleFactory()
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("vehicle-telemetry-current", kwargs={"vehicle_uuid": vehicle.uuid}))
        assert response.status_code == 403

    def test_authorized_user_gets_current_telemetry(self, client):
        vehicle = VehicleFactory()
        VehicleCurrentTelemetryFactory(vehicle=vehicle, speed="42.00", latitude="12.971600", longitude="77.594600")
        viewer = UserFactory(role=_view_role())
        client.force_login(viewer)
        response = client.get(reverse("vehicle-telemetry-current", kwargs={"vehicle_uuid": vehicle.uuid}))
        assert response.status_code == 200
        body = response.json()
        assert body["speed"] == "42.00"
        assert body["connection_status"] == "ONLINE"

    def test_no_telemetry_returns_offline_and_no_crash(self, client):
        vehicle = VehicleFactory()
        viewer = UserFactory(role=_view_role())
        client.force_login(viewer)
        response = client.get(reverse("vehicle-telemetry-current", kwargs={"vehicle_uuid": vehicle.uuid}))
        assert response.status_code == 200
        assert response.json()["connection_status"] == "OFFLINE"


class TestVehicleTelemetryHistoryAPI:
    def test_requires_tracking_device_view_permission(self, client):
        vehicle = VehicleFactory()
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("vehicle-telemetry-history", kwargs={"vehicle_uuid": vehicle.uuid}))
        assert response.status_code == 403

    def test_returns_history_for_vehicle(self, client):
        vehicle = VehicleFactory()
        TelemetryEventFactory(vehicle=vehicle, timestamp=timezone.now())
        TelemetryEventFactory(vehicle=vehicle, timestamp=timezone.now() - datetime.timedelta(hours=1))
        viewer = UserFactory(role=_view_role())
        client.force_login(viewer)
        response = client.get(reverse("vehicle-telemetry-history", kwargs={"vehicle_uuid": vehicle.uuid}))
        assert response.status_code == 200
        assert response.json()["count"] == 2

    def test_from_to_filtering(self, client):
        vehicle = VehicleFactory()
        now = timezone.now()
        TelemetryEventFactory(vehicle=vehicle, timestamp=now - datetime.timedelta(days=2))
        recent = TelemetryEventFactory(vehicle=vehicle, timestamp=now)
        viewer = UserFactory(role=_view_role())
        client.force_login(viewer)
        response = client.get(
            reverse("vehicle-telemetry-history", kwargs={"vehicle_uuid": vehicle.uuid}),
            {"from": (now - datetime.timedelta(hours=1)).isoformat()},
        )
        assert response.status_code == 200
        assert response.json()["count"] == 1

    def test_limit_enforced_and_hard_capped(self, client):
        vehicle = VehicleFactory()
        for i in range(5):
            TelemetryEventFactory(vehicle=vehicle, timestamp=timezone.now() - datetime.timedelta(minutes=i))
        viewer = UserFactory(role=_view_role())
        client.force_login(viewer)

        response = client.get(
            reverse("vehicle-telemetry-history", kwargs={"vehicle_uuid": vehicle.uuid}), {"limit": 2}
        )
        assert response.json()["count"] == 2

        # requesting far beyond the hard cap must still be bounded, never "unlimited"
        response = client.get(
            reverse("vehicle-telemetry-history", kwargs={"vehicle_uuid": vehicle.uuid}), {"limit": 999999}
        )
        assert response.json()["limit"] == 1000


class TestFleetCurrentTelemetryAPI:
    def test_requires_authentication(self, client):
        response = client.get(reverse("fleet-telemetry-current"))
        assert response.status_code in (401, 403)

    def test_requires_tracking_device_view_permission_and_leaks_no_coordinates(self, client):
        vehicle = VehicleFactory()
        VehicleCurrentTelemetryFactory(vehicle=vehicle, latitude="12.971600", longitude="77.594600")
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("fleet-telemetry-current"))
        assert response.status_code == 403
        assert b"12.9716" not in response.content
        assert b"latitude" not in response.content

    def test_authorized_user_gets_fleet_payload(self, client):
        vehicle = VehicleFactory()
        VehicleCurrentTelemetryFactory(
            vehicle=vehicle, latitude="12.971600", longitude="77.594600", speed="42.00"
        )
        viewer = UserFactory(role=_view_role())
        client.force_login(viewer)
        response = client.get(reverse("fleet-telemetry-current"))
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 1
        item = body["results"][0]
        assert item["vehicle_uuid"] == str(vehicle.uuid)
        assert item["registration_number"] == vehicle.registration_number
        assert item["latitude"] == "12.971600"
        assert item["connection_status"] == "ONLINE"
        assert item["driver_name"] is None

    def test_driver_name_present_when_vehicle_has_current_driver(self, client):
        from apps.drivers.tests.factories import DriverFactory

        driver = DriverFactory(first_name="Vikram", last_name="Raj")
        vehicle = VehicleFactory(current_driver=driver)
        VehicleCurrentTelemetryFactory(vehicle=vehicle)
        viewer = UserFactory(role=_view_role())
        client.force_login(viewer)
        response = client.get(reverse("fleet-telemetry-current"))
        assert response.json()["results"][0]["driver_name"] == "Vikram Raj"

    def test_only_vehicles_with_telemetry_included(self, client):
        with_telemetry = VehicleFactory()
        VehicleCurrentTelemetryFactory(vehicle=with_telemetry)
        VehicleFactory()  # no telemetry
        viewer = UserFactory(role=_view_role())
        client.force_login(viewer)
        response = client.get(reverse("fleet-telemetry-current"))
        body = response.json()
        assert body["count"] == 1
        assert body["results"][0]["vehicle_uuid"] == str(with_telemetry.uuid)

    def test_no_telemetry_at_all_returns_empty_list(self, client):
        VehicleFactory()
        viewer = UserFactory(role=_view_role())
        client.force_login(viewer)
        response = client.get(reverse("fleet-telemetry-current"))
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 0 and body["results"] == []
        assert set(body) == {"count", "results", "sync"}  # `sync` = feed health, additive

    def test_query_count_does_not_grow_with_fleet_size(self, client):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        viewer = UserFactory(role=_view_role())
        client.force_login(viewer)

        VehicleCurrentTelemetryFactory(vehicle=VehicleFactory())
        with CaptureQueriesContext(connection) as small:
            response = client.get(reverse("fleet-telemetry-current"))
        assert response.status_code == 200

        for _ in range(20):
            VehicleCurrentTelemetryFactory(vehicle=VehicleFactory())
        with CaptureQueriesContext(connection) as large:
            response = client.get(reverse("fleet-telemetry-current"))
        assert response.status_code == 200

        assert len(large.captured_queries) == len(small.captured_queries)
