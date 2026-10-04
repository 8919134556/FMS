import pytest
from django.urls import reverse

from apps.accounts.models import Permission, RolePermission
from apps.accounts.tests.factories import RoleFactory, UserFactory
from apps.tracking.models import DeviceCommand, TrackingDevice
from apps.tracking.tests.factories import DeviceCommandFactory, TrackingDeviceFactory

pytestmark = pytest.mark.django_db


def _role_with(*module_actions):
    role = RoleFactory()
    for module, action in module_actions:
        permission, _ = Permission.objects.get_or_create(module=module, action=action)
        RolePermission.objects.create(role=role, permission=permission)
    return role


def _view_role():
    return _role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW))


def _view_and_update_role():
    return _role_with(
        (Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW),
        (Permission.Module.TRACKING_DEVICE, Permission.Action.UPDATE),
    )


def _command_role():
    return _role_with(
        (Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW),
        (Permission.Module.TRACKING_DEVICE, Permission.Action.COMMAND),
    )


class TestCommandListView:
    def test_anonymous_redirected(self, client):
        response = client.get(reverse("tracking:command_list"))
        assert response.status_code == 302

    def test_requires_view_permission(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("tracking:command_list"))
        assert response.status_code == 403

    def test_view_only_sees_list_without_send_button(self, client):
        viewer = UserFactory(role=_view_role())
        DeviceCommandFactory()
        client.force_login(viewer)
        response = client.get(reverse("tracking:command_list"))
        assert response.status_code == 200
        assert response.context["can_command"] is False
        assert b"Send Command" not in response.content

    def test_status_filter(self, client):
        viewer = UserFactory(role=_view_role())
        pending = DeviceCommandFactory(status=DeviceCommand.Status.PENDING)
        DeviceCommandFactory(status=DeviceCommand.Status.COMPLETED)
        client.force_login(viewer)
        response = client.get(reverse("tracking:command_list"), {"status": DeviceCommand.Status.PENDING})
        assert pending.device.imei.encode() in response.content


class TestCommandCreateView:
    def test_requires_command_permission_not_just_update(self, client):
        """A role with view+update on devices but no explicit command grant
        cannot send commands."""
        actor = UserFactory(role=_view_and_update_role())
        client.force_login(actor)
        response = client.get(reverse("tracking:command_create"))
        assert response.status_code == 403

    def test_command_role_can_create(self, client):
        actor = UserFactory(role=_command_role())
        device = TrackingDeviceFactory()
        client.force_login(actor)
        response = client.post(
            reverse("tracking:command_create"),
            {"device": device.id, "command_type": DeviceCommand.CommandType.PING},
        )
        assert response.status_code == 302
        command = DeviceCommand.objects.get(device=device)
        assert command.status == DeviceCommand.Status.PENDING
        assert command.created_by == actor

    def test_device_prefilled_from_query_param(self, client):
        actor = UserFactory(role=_command_role())
        device = TrackingDeviceFactory()
        client.force_login(actor)
        response = client.get(reverse("tracking:command_create"), {"device": str(device.uuid)})
        assert response.status_code == 200
        assert response.context["form"].initial.get("device") == device

    def test_set_reporting_interval_requires_interval_seconds(self, client):
        actor = UserFactory(role=_command_role())
        device = TrackingDeviceFactory()
        client.force_login(actor)
        response = client.post(
            reverse("tracking:command_create"),
            {"device": device.id, "command_type": DeviceCommand.CommandType.SET_REPORTING_INTERVAL},
        )
        assert response.status_code == 200  # re-rendered with errors
        assert not DeviceCommand.objects.filter(device=device).exists()

    def test_set_reporting_interval_builds_payload(self, client):
        actor = UserFactory(role=_command_role())
        device = TrackingDeviceFactory()
        client.force_login(actor)
        response = client.post(
            reverse("tracking:command_create"),
            {
                "device": device.id, "command_type": DeviceCommand.CommandType.SET_REPORTING_INTERVAL,
                "interval_seconds": "60",
            },
        )
        assert response.status_code == 302
        command = DeviceCommand.objects.get(device=device)
        assert command.payload == {"interval_seconds": 60}

    def test_custom_requires_valid_json_object(self, client):
        actor = UserFactory(role=_command_role())
        device = TrackingDeviceFactory()
        client.force_login(actor)
        response = client.post(
            reverse("tracking:command_create"),
            {"device": device.id, "command_type": DeviceCommand.CommandType.CUSTOM, "custom_payload": "not json"},
        )
        assert response.status_code == 200
        assert not DeviceCommand.objects.filter(device=device).exists()

    def test_set_reporting_interval_rejected_for_teltonika_device(self, client):
        """Capability validation (Phase 3.6): Teltonika's real protocol has
        no plain-text interval command, so the UI must refuse to queue one."""
        actor = UserFactory(role=_command_role())
        device = TrackingDeviceFactory(provider=TrackingDevice.Provider.TELTONIKA)
        client.force_login(actor)
        response = client.post(
            reverse("tracking:command_create"),
            {
                "device": device.id, "command_type": DeviceCommand.CommandType.SET_REPORTING_INTERVAL,
                "interval_seconds": "60",
            },
        )
        assert response.status_code == 200  # re-rendered with a capability error
        assert not DeviceCommand.objects.filter(device=device).exists()

    def test_set_reporting_interval_accepted_for_generic_device(self, client):
        actor = UserFactory(role=_command_role())
        device = TrackingDeviceFactory(provider=TrackingDevice.Provider.GENERIC)
        client.force_login(actor)
        response = client.post(
            reverse("tracking:command_create"),
            {
                "device": device.id, "command_type": DeviceCommand.CommandType.SET_REPORTING_INTERVAL,
                "interval_seconds": "60",
            },
        )
        assert response.status_code == 302
        assert DeviceCommand.objects.filter(device=device).exists()

    def test_ping_accepted_for_teltonika_device(self, client):
        actor = UserFactory(role=_command_role())
        device = TrackingDeviceFactory(provider=TrackingDevice.Provider.TELTONIKA)
        client.force_login(actor)
        response = client.post(
            reverse("tracking:command_create"),
            {"device": device.id, "command_type": DeviceCommand.CommandType.PING},
        )
        assert response.status_code == 302
        assert DeviceCommand.objects.filter(device=device).exists()

    def test_custom_command_text_stored_for_teltonika_device(self, client):
        actor = UserFactory(role=_command_role())
        device = TrackingDeviceFactory(provider=TrackingDevice.Provider.TELTONIKA)
        client.force_login(actor)
        response = client.post(
            reverse("tracking:command_create"),
            {"device": device.id, "command_type": DeviceCommand.CommandType.CUSTOM, "custom_payload": "getver"},
        )
        assert response.status_code == 302
        command = DeviceCommand.objects.get(device=device)
        assert command.payload == {"command_text": "getver"}

    def test_inactive_device_not_selectable(self, client):
        actor = UserFactory(role=_command_role())
        device = TrackingDeviceFactory(status="INACTIVE")
        client.force_login(actor)
        response = client.get(reverse("tracking:command_create"))
        assert device not in response.context["form"].fields["device"].queryset


class TestCommandDetailView:
    def test_requires_view_permission(self, client):
        actor = UserFactory(role=None)
        command = DeviceCommandFactory()
        client.force_login(actor)
        response = client.get(reverse("tracking:command_detail", kwargs={"uuid": command.uuid}))
        assert response.status_code == 403

    def test_can_cancel_flag_for_pending(self, client):
        viewer = UserFactory(role=_command_role())
        command = DeviceCommandFactory(status=DeviceCommand.Status.PENDING)
        client.force_login(viewer)
        response = client.get(reverse("tracking:command_detail", kwargs={"uuid": command.uuid}))
        assert response.context["can_cancel"] is True
        assert response.context["can_retry"] is False

    def test_can_retry_flag_for_failed_under_cap(self, client):
        viewer = UserFactory(role=_command_role())
        command = DeviceCommandFactory(status=DeviceCommand.Status.FAILED, retry_count=0, max_retries=3)
        client.force_login(viewer)
        response = client.get(reverse("tracking:command_detail", kwargs={"uuid": command.uuid}))
        assert response.context["can_retry"] is True

    def test_no_command_permission_hides_actions_even_if_eligible(self, client):
        viewer = UserFactory(role=_view_role())
        command = DeviceCommandFactory(status=DeviceCommand.Status.PENDING)
        client.force_login(viewer)
        response = client.get(reverse("tracking:command_detail", kwargs={"uuid": command.uuid}))
        assert response.context["can_cancel"] is False


class TestCommandCancelRetryViews:
    def test_cancel_requires_command_permission(self, client):
        actor = UserFactory(role=_view_and_update_role())
        command = DeviceCommandFactory(status=DeviceCommand.Status.PENDING)
        client.force_login(actor)
        response = client.post(reverse("tracking:command_cancel", kwargs={"uuid": command.uuid}))
        assert response.status_code == 403

    def test_cancel_succeeds_with_command_permission(self, client):
        actor = UserFactory(role=_command_role())
        command = DeviceCommandFactory(status=DeviceCommand.Status.PENDING)
        client.force_login(actor)
        response = client.post(reverse("tracking:command_cancel", kwargs={"uuid": command.uuid}))
        assert response.status_code == 302
        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.CANCELLED

    def test_retry_requires_command_permission(self, client):
        actor = UserFactory(role=_view_and_update_role())
        command = DeviceCommandFactory(status=DeviceCommand.Status.FAILED)
        client.force_login(actor)
        response = client.post(reverse("tracking:command_retry", kwargs={"uuid": command.uuid}))
        assert response.status_code == 403

    def test_retry_succeeds_with_command_permission(self, client):
        actor = UserFactory(role=_command_role())
        command = DeviceCommandFactory(status=DeviceCommand.Status.FAILED, retry_count=0, max_retries=3)
        client.force_login(actor)
        response = client.post(reverse("tracking:command_retry", kwargs={"uuid": command.uuid}))
        assert response.status_code == 302
        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.PENDING
