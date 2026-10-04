"""End-to-end smoke test for GPS Data Ingestion & Vehicle Telemetry
(Phase 3.3): device create -> key reveal -> assign -> device-authenticated
ingestion -> current telemetry -> Vehicle Detail GPS tab integration ->
unassign -> disable. Mirrors the smoke-test benchmark used by
apps.maintenance/apps.trips.
"""

import json

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Permission, RolePermission
from apps.accounts.tests.factories import RoleFactory, UserFactory
from apps.tracking.models import TrackingDevice
from apps.vehicles.models import Vehicle
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


def _full_access_role():
    role = RoleFactory()
    for module in [Permission.Module.TRACKING_DEVICE, Permission.Module.VEHICLE]:
        for action in [Permission.Action.VIEW, Permission.Action.CREATE, Permission.Action.UPDATE, Permission.Action.ARCHIVE]:
            permission, _ = Permission.objects.get_or_create(module=module, action=action)
            RolePermission.objects.create(role=role, permission=permission)
    return role


class TestTelemetryModuleSmoke:
    def test_full_lifecycle(self, client):
        role = _full_access_role()
        actor = UserFactory(role=role)
        client.force_login(actor)

        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE)

        # 1. Create device -> one-time key reveal.
        create_response = client.post(
            reverse("tracking:device_create"),
            {"name": "Truck Tracker", "imei": "868888888888880", "provider": TrackingDevice.Provider.GENERIC},
        )
        assert create_response.status_code == 302
        device = TrackingDevice.objects.get(imei="868888888888880")

        reveal_response = client.get(create_response.url)
        assert reveal_response.status_code == 200
        raw_key = reveal_response.context["raw_key"]
        assert raw_key

        # 2. Vehicle GPS tab shows the linked-but-unassigned device, no position yet.
        gps_before_assign = client.get(reverse("vehicles:vehicle_detail", kwargs={"uuid": vehicle.uuid}), {"tab": "gps"})
        assert b"No GPS device linked" in gps_before_assign.content

        # 3. Assign device to the vehicle.
        assign_response = client.post(
            reverse("tracking:device_assign", kwargs={"uuid": device.uuid}), {"vehicle": vehicle.id}
        )
        assert assign_response.status_code == 302
        device.refresh_from_db()
        assert device.vehicle_id == vehicle.pk

        gps_after_assign = client.get(reverse("vehicles:vehicle_detail", kwargs={"uuid": vehicle.uuid}), {"tab": "gps"})
        assert b"No telemetry received" in gps_after_assign.content

        # 4. Device ingests telemetry via the device-authenticated endpoint (not the human session).
        # Timestamp is "now" (not a hardcoded date) so the connection-status
        # assertion below (ONLINE) is stable regardless of wall-clock time.
        client.logout()
        event_timestamp = timezone.now().isoformat()
        ingest_response = client.post(
            reverse("telemetry-ingest"),
            data=json.dumps({"timestamp": event_timestamp, "latitude": 12.9716, "longitude": 77.5946, "speed": 45.2, "ignition": True}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"DeviceKey {device.uuid}:{raw_key}",
        )
        assert ingest_response.status_code == 201

        # 5. Human session sees the real position on the Vehicle Detail GPS tab.
        client.force_login(actor)
        gps_with_data = client.get(reverse("vehicles:vehicle_detail", kwargs={"uuid": vehicle.uuid}), {"tab": "gps"})
        assert b"12.971600" in gps_with_data.content
        assert b"ONLINE" in gps_with_data.content

        # 6. Device Detail also reflects the same position.
        device_detail = client.get(reverse("tracking:device_detail", kwargs={"uuid": device.uuid}))
        assert device_detail.status_code == 200
        assert b"12.971600" in device_detail.content

        # 7. Assigning a device never touches Vehicle.status/AvailabilityStatus.
        vehicle.refresh_from_db()
        assert vehicle.status == Vehicle.Status.ACTIVE

        # 8. Read API (RBAC'd, human session) confirms the same current state.
        current_api_response = client.get(reverse("vehicle-telemetry-current", kwargs={"vehicle_uuid": vehicle.uuid}))
        assert current_api_response.status_code == 200
        assert current_api_response.json()["connection_status"] == "ONLINE"

        history_api_response = client.get(reverse("vehicle-telemetry-history", kwargs={"vehicle_uuid": vehicle.uuid}))
        assert history_api_response.status_code == 200
        assert history_api_response.json()["count"] == 1

        # 9. Unassign, then disable — both audited, neither breaks the vehicle record.
        unassign_response = client.post(reverse("tracking:device_unassign", kwargs={"uuid": device.uuid}))
        assert unassign_response.status_code == 302
        device.refresh_from_db()
        assert device.vehicle_id is None

        disable_response = client.post(reverse("tracking:device_disable", kwargs={"uuid": device.uuid}))
        assert disable_response.status_code == 302
        device.refresh_from_db()
        assert device.status == TrackingDevice.Status.SUSPENDED

        # 10. A disabled device can no longer ingest.
        client.logout()
        blocked_ingest = client.post(
            reverse("telemetry-ingest"),
            data=json.dumps({"timestamp": "2026-08-21T11:00:00Z", "latitude": 12.0, "longitude": 77.0}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"DeviceKey {device.uuid}:{raw_key}",
        )
        assert blocked_ingest.status_code == 403

        # 11. Device list renders.
        client.force_login(actor)
        list_response = client.get(reverse("tracking:device_list"))
        assert list_response.status_code == 200
        assert b"868888888888880" in list_response.content
