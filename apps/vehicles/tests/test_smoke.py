"""End-to-end smoke test for the Vehicles module: every tab on the detail
page, the create/edit forms, and the dashboard's new real-data widgets all
have to render without error for the module to count as "functional",
not just individually-unit-tested.
"""

import datetime

import pytest
from django.urls import reverse

from apps.accounts.models import Permission, RolePermission
from apps.accounts.tests.factories import RoleFactory, UserFactory
from apps.drivers.tests.factories import DriverFactory
from apps.vehicles import services
from apps.vehicles.models import VehicleDriverAssignment
from apps.vehicles.tests.factories import FuelTypeFactory, VehicleFactory, VehicleTypeFactory

pytestmark = pytest.mark.django_db


def _full_access_role():
    role = RoleFactory()
    for module in [
        Permission.Module.VEHICLE, Permission.Module.DRIVER, Permission.Module.ASSIGNMENT,
        Permission.Module.USER, Permission.Module.ROLE,
    ]:
        for action in [
            Permission.Action.VIEW, Permission.Action.CREATE, Permission.Action.UPDATE,
            Permission.Action.ARCHIVE, Permission.Action.EXPORT,
        ]:
            permission, _ = Permission.objects.get_or_create(module=module, action=action)
            RolePermission.objects.create(role=role, permission=permission)
    return role


class TestVehicleModuleSmoke:
    def test_full_vehicle_lifecycle_and_all_detail_tabs_render(self, client):
        role = _full_access_role()
        actor = UserFactory(role=role)
        client.force_login(actor)

        vehicle_type = VehicleTypeFactory()
        fuel_type = FuelTypeFactory()

        create_response = client.post(
            reverse("vehicles:vehicle_create"),
            {
                "registration_number": "KA02SMOKE1",
                "vehicle_code": "SMOKE0001",
                "vehicle_type": vehicle_type.id,
                "fuel_type": fuel_type.id,
                "make": "Ashok Leyland",
                "model": "Dost",
                "status": "ACTIVE",
                "availability_status": "AVAILABLE",
                "odometer_reading": "1200",
                "odometer_unit": "KM",
                "ownership_type": "OWNED",
            },
        )
        assert create_response.status_code == 302
        vehicle = VehicleFactory._meta.model.objects.get(registration_number="KA02SMOKE1")

        edit_response = client.get(reverse("vehicles:vehicle_edit", kwargs={"uuid": vehicle.uuid}))
        assert edit_response.status_code == 200

        driver = DriverFactory()
        assign_response = client.post(
            reverse("vehicles:vehicle_assign_driver", kwargs={"uuid": vehicle.uuid}),
            {
                "driver": driver.id,
                "assignment_type": VehicleDriverAssignment.AssignmentType.PRIMARY,
                "primary_driver": "on",
                "start_date": datetime.date.today().isoformat(),
                "remarks": "smoke test",
            },
        )
        assert assign_response.status_code == 302

        for tab in ["overview", "driver", "gps", "maintenance", "documents", "history"]:
            url = reverse("vehicles:vehicle_detail", kwargs={"uuid": vehicle.uuid})
            response = client.get(url, {"tab": tab} if tab != "overview" else {})
            assert response.status_code == 200, f"tab={tab} failed"

        unassign_url = reverse(
            "vehicles:vehicle_unassign_driver",
            kwargs={"uuid": vehicle.uuid, "assignment_id": vehicle.driver_assignments.first().id},
        )
        unassign_response = client.post(unassign_url)
        assert unassign_response.status_code == 302

        deactivate_response = client.post(reverse("vehicles:vehicle_deactivate", kwargs={"uuid": vehicle.uuid}))
        assert deactivate_response.status_code == 302

        driver_detail_response = client.get(reverse("drivers:driver_detail", kwargs={"uuid": driver.uuid}))
        assert driver_detail_response.status_code == 200

        list_response = client.get(reverse("vehicles:vehicle_list"))
        assert list_response.status_code == 200

        export_response = client.get(reverse("vehicles:vehicle_export"))
        assert export_response.status_code == 200
        assert export_response["Content-Type"] == "text/csv"

    def test_dashboard_renders_with_real_vehicle_driver_data(self, client):
        role = _full_access_role()
        actor = UserFactory(role=role)
        VehicleFactory.create_batch(3)
        DriverFactory.create_batch(2)
        client.force_login(actor)

        response = client.get(reverse("core:dashboard"))
        assert response.status_code == 200
        assert b"Total Vehicles" in response.content
        assert b"Active Drivers" in response.content

    def test_dashboard_renders_with_no_vehicles(self, client):
        role = _full_access_role()
        actor = UserFactory(role=role)
        client.force_login(actor)

        response = client.get(reverse("core:dashboard"))
        assert response.status_code == 200
