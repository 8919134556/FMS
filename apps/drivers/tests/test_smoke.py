"""End-to-end smoke test for the Drivers module: every tab on the detail
page, the create/edit forms, and the full assign-vehicle workflow all have
to render and work together without error — mirrors
apps.vehicles.tests.test_smoke, which is the quality benchmark this module
was built to match.
"""

import datetime

import pytest
from django.urls import reverse

from apps.accounts.models import Permission, RolePermission
from apps.accounts.tests.factories import RoleFactory, UserFactory
from apps.drivers.models import Driver
from apps.vehicles.models import Vehicle, VehicleDriverAssignment
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


def _full_access_role():
    role = RoleFactory()
    for module in [Permission.Module.DRIVER, Permission.Module.VEHICLE, Permission.Module.ASSIGNMENT]:
        for action in [
            Permission.Action.VIEW, Permission.Action.CREATE, Permission.Action.UPDATE,
            Permission.Action.ARCHIVE, Permission.Action.EXPORT,
        ]:
            permission, _ = Permission.objects.get_or_create(module=module, action=action)
            RolePermission.objects.create(role=role, permission=permission)
    return role


class TestDriverModuleSmoke:
    def test_full_driver_lifecycle_and_all_detail_tabs_render(self, client):
        role = _full_access_role()
        actor = UserFactory(role=role)
        client.force_login(actor)

        create_response = client.post(
            reverse("drivers:driver_create"),
            {
                "employee_id": "DRVSMOKE01",
                "first_name": "Smoke",
                "last_name": "Test",
                "mobile_number": "+15558887777",
                "license_number": "LICSMOKE01",
                "license_expiry_date": (datetime.date.today() + datetime.timedelta(days=10)).isoformat(),
                "driver_type": Driver.DriverType.COMPANY,
                "employment_type": Driver.EmploymentType.FULL_TIME,
                "employment_status": Driver.EmploymentStatus.ACTIVE,
            },
        )
        assert create_response.status_code == 302
        driver = Driver.objects.get(employee_id="DRVSMOKE01")
        assert driver.license_expiry_status == "EXPIRING_SOON"

        edit_response = client.get(reverse("drivers:driver_edit", kwargs={"uuid": driver.uuid}))
        assert edit_response.status_code == 200

        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE)
        assign_response = client.post(
            reverse("drivers:driver_assign_vehicle", kwargs={"uuid": driver.uuid}),
            {
                "vehicle": vehicle.id,
                "assignment_type": VehicleDriverAssignment.AssignmentType.PRIMARY,
                "primary_driver": "on",
                "start_date": datetime.date.today().isoformat(),
                "remarks": "smoke test",
            },
        )
        assert assign_response.status_code == 302

        for tab in ["overview", "license", "documents", "vehicles", "trips", "performance", "history"]:
            url = reverse("drivers:driver_detail", kwargs={"uuid": driver.uuid})
            response = client.get(url, {"tab": tab} if tab != "overview" else {})
            assert response.status_code == 200, f"tab={tab} failed"

        # License tab should surface the expiring-soon badge somewhere on the page.
        license_response = client.get(reverse("drivers:driver_detail", kwargs={"uuid": driver.uuid}), {"tab": "license"})
        assert b"Expiring Soon" in license_response.content

        unassign_url = reverse(
            "drivers:driver_unassign_vehicle",
            kwargs={"uuid": driver.uuid, "assignment_id": driver.vehicle_assignments.first().id},
        )
        unassign_response = client.post(unassign_url)
        assert unassign_response.status_code == 302

        deactivate_response = client.post(reverse("drivers:driver_deactivate", kwargs={"uuid": driver.uuid}))
        assert deactivate_response.status_code == 302

        vehicle_detail_response = client.get(reverse("vehicles:vehicle_detail", kwargs={"uuid": vehicle.uuid}))
        assert vehicle_detail_response.status_code == 200

        list_response = client.get(reverse("drivers:driver_list"))
        assert list_response.status_code == 200

        export_response = client.get(reverse("drivers:driver_export"))
        assert export_response.status_code == 200
        assert export_response["Content-Type"] == "text/csv"
