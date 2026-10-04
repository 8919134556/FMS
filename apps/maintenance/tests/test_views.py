import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Permission, RolePermission
from apps.accounts.tests.factories import RoleFactory, UserFactory
from apps.maintenance.models import Maintenance
from apps.maintenance.tests.factories import MaintenanceFactory
from apps.vehicles.models import Vehicle
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


def _role_with(*module_actions):
    role = RoleFactory()
    for module, action in module_actions:
        permission, _ = Permission.objects.get_or_create(module=module, action=action)
        RolePermission.objects.create(role=role, permission=permission)
    return role


def _full_maintenance_role():
    return _role_with(
        (Permission.Module.MAINTENANCE, Permission.Action.VIEW),
        (Permission.Module.MAINTENANCE, Permission.Action.CREATE),
        (Permission.Module.MAINTENANCE, Permission.Action.UPDATE),
        (Permission.Module.MAINTENANCE, Permission.Action.ARCHIVE),
        (Permission.Module.MAINTENANCE, Permission.Action.EXPORT),
    )


def _formset_management_data(prefix="parts"):
    return {
        f"{prefix}-TOTAL_FORMS": "0",
        f"{prefix}-INITIAL_FORMS": "0",
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "1000",
    }


class TestMaintenanceListView:
    def test_anonymous_redirected_to_login(self, client):
        response = client.get(reverse("maintenance:maintenance_list"))
        assert response.status_code == 302

    def test_user_without_permission_gets_403(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("maintenance:maintenance_list"))
        assert response.status_code == 403

    def test_user_with_view_permission_sees_list(self, client):
        role = _role_with((Permission.Module.MAINTENANCE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        MaintenanceFactory.create_batch(2)
        client.force_login(viewer)
        response = client.get(reverse("maintenance:maintenance_list"))
        assert response.status_code == 200
        assert b"Maintenance" in response.content

    def test_search_by_maintenance_number(self, client):
        role = _role_with((Permission.Module.MAINTENANCE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        MaintenanceFactory(maintenance_number="MNT-777777")
        client.force_login(viewer)
        response = client.get(reverse("maintenance:maintenance_list"), {"q": "MNT-777777"})
        assert response.status_code == 200
        assert b"MNT-777777" in response.content

    def test_status_filter(self, client):
        role = _role_with((Permission.Module.MAINTENANCE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        MaintenanceFactory(maintenance_number="MNT-888801", status=Maintenance.Status.CANCELLED)
        MaintenanceFactory(maintenance_number="MNT-888802", status=Maintenance.Status.SCHEDULED)
        client.force_login(viewer)
        response = client.get(reverse("maintenance:maintenance_list"), {"status": Maintenance.Status.CANCELLED})
        assert b"MNT-888801" in response.content
        assert b"MNT-888802" not in response.content

    def test_overdue_status_filter_uses_computed_logic(self, client):
        role = _role_with((Permission.Module.MAINTENANCE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        MaintenanceFactory(
            maintenance_number="MNT-OVERDUE1",
            scheduled_date=timezone.now().date() - datetime.timedelta(days=2),
        )
        MaintenanceFactory(
            maintenance_number="MNT-FUTURE01",
            scheduled_date=timezone.now().date() + datetime.timedelta(days=10),
        )
        client.force_login(viewer)
        response = client.get(reverse("maintenance:maintenance_list"), {"status": "OVERDUE"})
        assert b"MNT-OVERDUE1" in response.content
        assert b"MNT-FUTURE01" not in response.content

    def test_vehicle_filter(self, client):
        role = _role_with((Permission.Module.MAINTENANCE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        target_vehicle = VehicleFactory()
        MaintenanceFactory(maintenance_number="MNT-VEHFLT01", vehicle=target_vehicle)
        MaintenanceFactory(maintenance_number="MNT-VEHFLT02")
        client.force_login(viewer)
        response = client.get(reverse("maintenance:maintenance_list"), {"vehicle": target_vehicle.id})
        assert b"MNT-VEHFLT01" in response.content
        assert b"MNT-VEHFLT02" not in response.content

    def test_pagination(self, client):
        role = _role_with((Permission.Module.MAINTENANCE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        MaintenanceFactory.create_batch(30)
        client.force_login(viewer)
        response = client.get(reverse("maintenance:maintenance_list"))
        assert response.status_code == 200
        assert response.context["page_obj"].has_next()

    def test_export_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("maintenance:maintenance_export"))
        assert response.status_code == 403

    def test_export_returns_csv(self, client):
        role = _role_with((Permission.Module.MAINTENANCE, Permission.Action.EXPORT))
        actor = UserFactory(role=role)
        MaintenanceFactory(maintenance_number="MNT-999901")
        client.force_login(actor)
        response = client.get(reverse("maintenance:maintenance_export"))
        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv"
        assert b"MNT-999901" in response.content

    def test_empty_state_no_records(self, client):
        role = _role_with((Permission.Module.MAINTENANCE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        client.force_login(viewer)
        response = client.get(reverse("maintenance:maintenance_list"))
        assert response.status_code == 200
        assert b"No maintenance records yet" in response.content


class TestMaintenanceCreateView:
    def test_create_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("maintenance:maintenance_create"))
        assert response.status_code == 403

    def test_create_success(self, client):
        role = _full_maintenance_role()
        actor = UserFactory(role=role)
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE)
        client.force_login(actor)
        response = client.post(
            reverse("maintenance:maintenance_create"),
            {
                "vehicle": vehicle.id,
                "maintenance_type": Maintenance.MaintenanceType.OIL_SERVICE,
                "priority": Maintenance.Priority.MEDIUM,
                "scheduled_date": "2030-01-01",
                "odometer_at_service": "12000",
                **_formset_management_data(),
            },
        )
        assert response.status_code == 302
        maintenance = Maintenance.objects.get(vehicle=vehicle)
        assert maintenance.status == Maintenance.Status.SCHEDULED
        assert maintenance.maintenance_number.startswith("MNT-")

    def test_create_prefills_vehicle_from_query_param(self, client):
        role = _full_maintenance_role()
        actor = UserFactory(role=role)
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE)
        client.force_login(actor)
        response = client.get(reverse("maintenance:maintenance_create"), {"vehicle": vehicle.id})
        assert response.status_code == 200
        assert str(vehicle.id).encode() in response.content

    def test_create_with_parts(self, client):
        role = _full_maintenance_role()
        actor = UserFactory(role=role)
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE)
        client.force_login(actor)
        data = {
            "vehicle": vehicle.id,
            "maintenance_type": Maintenance.MaintenanceType.REPAIR,
            "priority": Maintenance.Priority.HIGH,
            "scheduled_date": "2030-01-01",
            "odometer_at_service": "12000",
            "parts-TOTAL_FORMS": "1",
            "parts-INITIAL_FORMS": "0",
            "parts-MIN_NUM_FORMS": "0",
            "parts-MAX_NUM_FORMS": "1000",
            "parts-0-part_name": "Brake Pad",
            "parts-0-part_number": "BP-100",
            "parts-0-quantity": "2",
            "parts-0-unit_cost": "45.00",
        }
        response = client.post(reverse("maintenance:maintenance_create"), data)
        assert response.status_code == 302
        maintenance = Maintenance.objects.get(vehicle=vehicle)
        assert maintenance.parts.count() == 1
        assert maintenance.parts_cost == 90


class TestMaintenanceDetailView:
    def test_detail_requires_permission(self, client):
        actor = UserFactory(role=None)
        maintenance = MaintenanceFactory()
        client.force_login(actor)
        response = client.get(reverse("maintenance:maintenance_detail", kwargs={"uuid": maintenance.uuid}))
        assert response.status_code == 403

    def test_all_tabs_render(self, client):
        role = _role_with((Permission.Module.MAINTENANCE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        maintenance = MaintenanceFactory(maintenance_number="MNT-TABTEST")
        client.force_login(viewer)
        for tab in ["overview", "service", "parts", "costs", "documents", "history"]:
            url = reverse("maintenance:maintenance_detail", kwargs={"uuid": maintenance.uuid})
            response = client.get(url, {"tab": tab} if tab != "overview" else {})
            assert response.status_code == 200, f"tab={tab} failed"


class TestMaintenanceWorkflowViews:
    def test_start_view(self, client):
        role = _full_maintenance_role()
        actor = UserFactory(role=role)
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE)
        maintenance = MaintenanceFactory(vehicle=vehicle, status=Maintenance.Status.SCHEDULED)
        client.force_login(actor)
        response = client.post(reverse("maintenance:maintenance_start", kwargs={"uuid": maintenance.uuid}))
        assert response.status_code == 302
        maintenance.refresh_from_db()
        assert maintenance.status == Maintenance.Status.IN_PROGRESS

    def test_start_requires_permission(self, client):
        actor = UserFactory(role=None)
        maintenance = MaintenanceFactory(status=Maintenance.Status.SCHEDULED)
        client.force_login(actor)
        response = client.post(reverse("maintenance:maintenance_start", kwargs={"uuid": maintenance.uuid}))
        assert response.status_code == 403

    def test_complete_view(self, client):
        role = _full_maintenance_role()
        actor = UserFactory(role=role)
        maintenance = MaintenanceFactory(status=Maintenance.Status.IN_PROGRESS, started_date=timezone.now())
        client.force_login(actor)
        response = client.post(
            reverse("maintenance:maintenance_complete", kwargs={"uuid": maintenance.uuid}),
            {
                "completed_date": timezone.now().strftime("%Y-%m-%dT%H:%M"),
                "final_odometer": "12500",
                "actual_cost": "2000",
                "labor_cost": "500",
                "work_performed": "Replaced brake pads",
                "completion_notes": "Done",
            },
        )
        assert response.status_code == 302
        maintenance.refresh_from_db()
        assert maintenance.status == Maintenance.Status.COMPLETED

    def test_cancel_requires_reason(self, client):
        role = _full_maintenance_role()
        actor = UserFactory(role=role)
        maintenance = MaintenanceFactory(status=Maintenance.Status.SCHEDULED)
        client.force_login(actor)
        response = client.post(
            reverse("maintenance:maintenance_cancel", kwargs={"uuid": maintenance.uuid}), {"cancellation_reason": ""}
        )
        assert response.status_code == 302
        maintenance.refresh_from_db()
        assert maintenance.status == Maintenance.Status.SCHEDULED

    def test_cancel_success(self, client):
        role = _full_maintenance_role()
        actor = UserFactory(role=role)
        maintenance = MaintenanceFactory(status=Maintenance.Status.SCHEDULED)
        client.force_login(actor)
        response = client.post(
            reverse("maintenance:maintenance_cancel", kwargs={"uuid": maintenance.uuid}),
            {"cancellation_reason": "Vehicle sold"},
        )
        assert response.status_code == 302
        maintenance.refresh_from_db()
        assert maintenance.status == Maintenance.Status.CANCELLED

    def test_cancel_requires_archive_permission(self, client):
        role = _role_with(
            (Permission.Module.MAINTENANCE, Permission.Action.VIEW),
            (Permission.Module.MAINTENANCE, Permission.Action.UPDATE),
        )
        actor = UserFactory(role=role)
        maintenance = MaintenanceFactory(status=Maintenance.Status.SCHEDULED)
        client.force_login(actor)
        response = client.post(
            reverse("maintenance:maintenance_cancel", kwargs={"uuid": maintenance.uuid}),
            {"cancellation_reason": "test"},
        )
        assert response.status_code == 403


class TestServiceScheduleView:
    def test_anonymous_redirected_to_login(self, client):
        response = client.get(reverse("maintenance:service_schedule"))
        assert response.status_code == 302

    def test_user_without_permission_gets_403(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("maintenance:service_schedule"))
        assert response.status_code == 403

    def test_overdue_bucket(self, client):
        role = _role_with((Permission.Module.MAINTENANCE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        overdue = MaintenanceFactory(
            status=Maintenance.Status.SCHEDULED, scheduled_date=timezone.now().date() - datetime.timedelta(days=3)
        )
        client.force_login(viewer)
        response = client.get(reverse("maintenance:service_schedule"))
        assert response.status_code == 200
        assert list(response.context["overdue"]) == [overdue]

    def test_due_today_bucket(self, client):
        role = _role_with((Permission.Module.MAINTENANCE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        today_item = MaintenanceFactory(status=Maintenance.Status.SCHEDULED, scheduled_date=timezone.now().date())
        client.force_login(viewer)
        response = client.get(reverse("maintenance:service_schedule"))
        assert list(response.context["due_today"]) == [today_item]

    def test_due_this_week_bucket(self, client):
        role = _role_with((Permission.Module.MAINTENANCE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        soon_item = MaintenanceFactory(
            status=Maintenance.Status.SCHEDULED, scheduled_date=timezone.now().date() + datetime.timedelta(days=3)
        )
        client.force_login(viewer)
        response = client.get(reverse("maintenance:service_schedule"))
        assert list(response.context["due_this_week"]) == [soon_item]

    def test_upcoming_bucket_beyond_window(self, client):
        role = _role_with((Permission.Module.MAINTENANCE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        later_item = MaintenanceFactory(
            status=Maintenance.Status.SCHEDULED, scheduled_date=timezone.now().date() + datetime.timedelta(days=30)
        )
        client.force_login(viewer)
        response = client.get(reverse("maintenance:service_schedule"))
        assert list(response.context["upcoming"]) == [later_item]

    def test_vehicle_filter_scopes_all_buckets(self, client):
        role = _role_with((Permission.Module.MAINTENANCE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        target_vehicle = VehicleFactory()
        matching = MaintenanceFactory(
            vehicle=target_vehicle, status=Maintenance.Status.SCHEDULED,
            scheduled_date=timezone.now().date() - datetime.timedelta(days=1),
        )
        MaintenanceFactory(status=Maintenance.Status.SCHEDULED, scheduled_date=timezone.now().date() - datetime.timedelta(days=1))
        client.force_login(viewer)
        response = client.get(reverse("maintenance:service_schedule"), {"vehicle": target_vehicle.id})
        assert list(response.context["overdue"]) == [matching]

    def test_completed_maintenance_excluded_from_all_buckets(self, client):
        role = _role_with((Permission.Module.MAINTENANCE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        MaintenanceFactory(
            status=Maintenance.Status.COMPLETED, scheduled_date=timezone.now().date() - datetime.timedelta(days=1)
        )
        client.force_login(viewer)
        response = client.get(reverse("maintenance:service_schedule"))
        assert list(response.context["overdue"]) == []
