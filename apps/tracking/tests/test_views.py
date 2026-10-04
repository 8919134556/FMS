import pytest
from django.urls import reverse

from apps.accounts.models import Permission, RolePermission
from apps.accounts.tests.factories import RoleFactory, UserFactory
from apps.tracking.models import TrackingDevice
from apps.tracking.tests.factories import TrackingDeviceFactory
from apps.vehicles.models import Vehicle
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


def _role_with(*module_actions):
    role = RoleFactory()
    for module, action in module_actions:
        permission, _ = Permission.objects.get_or_create(module=module, action=action)
        RolePermission.objects.create(role=role, permission=permission)
    return role


def _full_role():
    return _role_with(
        (Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW),
        (Permission.Module.TRACKING_DEVICE, Permission.Action.CREATE),
        (Permission.Module.TRACKING_DEVICE, Permission.Action.UPDATE),
        (Permission.Module.TRACKING_DEVICE, Permission.Action.ARCHIVE),
    )


class TestDeviceListView:
    def test_anonymous_redirected(self, client):
        response = client.get(reverse("tracking:device_list"))
        assert response.status_code == 302

    def test_requires_view_permission(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("tracking:device_list"))
        assert response.status_code == 403

    def test_lists_devices(self, client):
        viewer = UserFactory(role=_role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW)))
        TrackingDeviceFactory(imei="999888777001")
        client.force_login(viewer)
        response = client.get(reverse("tracking:device_list"))
        assert response.status_code == 200
        assert b"999888777001" in response.content

    def test_empty_state(self, client):
        viewer = UserFactory(role=_role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW)))
        client.force_login(viewer)
        response = client.get(reverse("tracking:device_list"))
        assert b"No devices registered." in response.content

    def test_provider_filter(self, client):
        viewer = UserFactory(role=_role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW)))
        TrackingDeviceFactory(imei="700000001112223", provider=TrackingDevice.Provider.GENERIC)
        TrackingDeviceFactory(imei="700000002223334", provider=TrackingDevice.Provider.TELTONIKA)
        client.force_login(viewer)
        response = client.get(reverse("tracking:device_list"), {"provider": TrackingDevice.Provider.TELTONIKA})
        assert b"700000002223334" in response.content
        assert b"700000001112223" not in response.content

    def test_assigned_unassigned_filter(self, client):
        viewer = UserFactory(role=_role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW)))
        vehicle = VehicleFactory()
        TrackingDeviceFactory(imei="700000003334445", vehicle=vehicle)
        TrackingDeviceFactory(imei="700000004445556")
        client.force_login(viewer)
        response = client.get(reverse("tracking:device_list"), {"assigned": "yes"})
        assert b"700000003334445" in response.content
        assert b"700000004445556" not in response.content


class TestDeviceCreateView:
    def test_requires_create_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("tracking:device_create"))
        assert response.status_code == 403

    def test_create_device_issues_key_and_redirects_to_reveal(self, client):
        actor = UserFactory(role=_full_role())
        client.force_login(actor)
        response = client.post(
            reverse("tracking:device_create"),
            {"name": "Truck 1 Tracker", "imei": "356938035643809", "provider": TrackingDevice.Provider.GENERIC},
        )
        assert response.status_code == 302
        device = TrackingDevice.objects.get(imei="356938035643809")
        assert device.secret_hash
        assert response.url == reverse("tracking:device_key_reveal", kwargs={"uuid": device.uuid})

    def test_imei_uniqueness_enforced_by_form(self, client):
        actor = UserFactory(role=_full_role())
        TrackingDeviceFactory(imei="111222333444555")
        client.force_login(actor)
        response = client.post(
            reverse("tracking:device_create"),
            {"name": "Dup", "imei": "111222333444555", "provider": TrackingDevice.Provider.GENERIC},
        )
        assert response.status_code == 200  # form re-rendered with errors
        assert TrackingDevice.objects.filter(imei="111222333444555").count() == 1


class TestDeviceKeyReveal:
    def test_key_shown_once_then_gone(self, client):
        actor = UserFactory(role=_full_role())
        client.force_login(actor)
        create_response = client.post(
            reverse("tracking:device_create"),
            {"name": "Tracker", "imei": "111111111111111", "provider": TrackingDevice.Provider.GENERIC},
        )
        reveal_url = create_response.url
        first_view = client.get(reveal_url)
        assert first_view.status_code == 200
        assert b"DeviceKey" in first_view.content

        second_view = client.get(reveal_url)
        assert second_view.status_code == 302  # already viewed, redirected away


class TestDeviceAssignUnassign:
    def test_assign_success(self, client):
        actor = UserFactory(role=_full_role())
        device = TrackingDeviceFactory()
        vehicle = VehicleFactory()
        client.force_login(actor)
        response = client.post(reverse("tracking:device_assign", kwargs={"uuid": device.uuid}), {"vehicle": vehicle.id})
        assert response.status_code == 302
        device.refresh_from_db()
        assert device.vehicle_id == vehicle.pk

    def test_unassign_success(self, client):
        actor = UserFactory(role=_full_role())
        vehicle = VehicleFactory()
        device = TrackingDeviceFactory(vehicle=vehicle)
        client.force_login(actor)
        response = client.post(reverse("tracking:device_unassign", kwargs={"uuid": device.uuid}))
        assert response.status_code == 302
        device.refresh_from_db()
        assert device.vehicle_id is None

    def test_assign_requires_update_permission(self, client):
        actor = UserFactory(role=None)
        device = TrackingDeviceFactory()
        vehicle = VehicleFactory()
        client.force_login(actor)
        response = client.post(reverse("tracking:device_assign", kwargs={"uuid": device.uuid}), {"vehicle": vehicle.id})
        assert response.status_code == 403


class TestDeviceDisableEnable:
    def test_disable_requires_archive_permission(self, client):
        actor = UserFactory(role=_role_with(
            (Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW),
            (Permission.Module.TRACKING_DEVICE, Permission.Action.UPDATE),
        ))
        device = TrackingDeviceFactory()
        client.force_login(actor)
        response = client.post(reverse("tracking:device_disable", kwargs={"uuid": device.uuid}))
        assert response.status_code == 403

    def test_disable_then_enable(self, client):
        actor = UserFactory(role=_full_role())
        device = TrackingDeviceFactory(status=TrackingDevice.Status.ACTIVE)
        client.force_login(actor)

        disable_response = client.post(reverse("tracking:device_disable", kwargs={"uuid": device.uuid}))
        assert disable_response.status_code == 302
        device.refresh_from_db()
        assert device.status == TrackingDevice.Status.SUSPENDED

        enable_response = client.post(reverse("tracking:device_enable", kwargs={"uuid": device.uuid}))
        assert enable_response.status_code == 302
        device.refresh_from_db()
        assert device.status == TrackingDevice.Status.ACTIVE


class TestDeviceDetailView:
    def test_requires_view_permission(self, client):
        actor = UserFactory(role=None)
        device = TrackingDeviceFactory()
        client.force_login(actor)
        response = client.get(reverse("tracking:device_detail", kwargs={"uuid": device.uuid}))
        assert response.status_code == 403

    def test_renders_with_no_telemetry(self, client):
        viewer = UserFactory(role=_role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW)))
        vehicle = VehicleFactory()
        device = TrackingDeviceFactory(vehicle=vehicle)
        client.force_login(viewer)
        response = client.get(reverse("tracking:device_detail", kwargs={"uuid": device.uuid}))
        assert response.status_code == 200
        assert b"No recent position available" in response.content


class TestLiveTrackingView:
    def test_anonymous_redirected(self, client):
        response = client.get(reverse("tracking:live_map"))
        assert response.status_code == 302

    def test_requires_view_permission(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("tracking:live_map"))
        assert response.status_code == 403

    def test_renders_with_permission(self, client):
        viewer = UserFactory(role=_role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW)))
        client.force_login(viewer)
        response = client.get(reverse("tracking:live_map"))
        assert response.status_code == 200
        assert b"Live Tracking" in response.content

    def test_valid_vehicle_param_passed_to_context(self, client):
        viewer = UserFactory(role=_role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW)))
        vehicle = VehicleFactory()
        client.force_login(viewer)
        response = client.get(reverse("tracking:live_map"), {"vehicle": str(vehicle.uuid)})
        assert response.context["initial_vehicle_uuid"] == str(vehicle.uuid)

    def test_garbage_vehicle_param_silently_dropped(self, client):
        viewer = UserFactory(role=_role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW)))
        client.force_login(viewer)
        response = client.get(reverse("tracking:live_map"), {"vehicle": "not-a-uuid"})
        assert response.status_code == 200
        assert response.context["initial_vehicle_uuid"] == ""

    def test_nonexistent_vehicle_uuid_silently_dropped(self, client):
        viewer = UserFactory(role=_role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW)))
        client.force_login(viewer)
        response = client.get(
            reverse("tracking:live_map"), {"vehicle": "00000000-0000-0000-0000-000000000000"}
        )
        assert response.status_code == 200
        assert response.context["initial_vehicle_uuid"] == ""

    def test_valid_status_param_accepted(self, client):
        viewer = UserFactory(role=_role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW)))
        client.force_login(viewer)
        response = client.get(reverse("tracking:live_map"), {"status": "online"})
        assert response.context["initial_status"] == "online"

    def test_invalid_status_param_dropped(self, client):
        viewer = UserFactory(role=_role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW)))
        client.force_login(viewer)
        response = client.get(reverse("tracking:live_map"), {"status": "garbage"})
        assert response.status_code == 200
        assert response.context["initial_status"] == ""

    @pytest.mark.parametrize("status", ["moving", "idle"])
    def test_movement_status_param_accepted(self, client, status):
        viewer = UserFactory(role=_role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW)))
        client.force_login(viewer)
        response = client.get(reverse("tracking:live_map"), {"status": status})
        assert response.context["initial_status"] == status

    def test_page_embeds_no_coordinates_server_side(self, client):
        """Proves the map page fetches positions client-side only — even an
        authorized viewer's page source must not contain a real telemetry
        coordinate."""
        from apps.tracking.tests.factories import VehicleCurrentTelemetryFactory

        viewer = UserFactory(role=_role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW)))
        vehicle = VehicleFactory()
        VehicleCurrentTelemetryFactory(vehicle=vehicle, latitude="12.971600", longitude="77.594600")
        client.force_login(viewer)
        response = client.get(reverse("tracking:live_map"))
        assert response.status_code == 200
        assert b"12.9716" not in response.content
        assert b"77.5946" not in response.content
