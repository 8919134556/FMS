import datetime

import pytest
from django.urls import reverse

from apps.accounts.models import Permission, RolePermission
from apps.accounts.tests.factories import RoleFactory, UserFactory
from apps.clients.models import Client
from apps.drivers.models import Driver
from apps.drivers.tests.factories import DriverFactory
from apps.locations.models import Branch
from apps.vehicles.models import Vehicle, VehicleDriverAssignment
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


def _role_with(*module_actions):
    role = RoleFactory()
    for module, action in module_actions:
        permission, _ = Permission.objects.get_or_create(module=module, action=action)
        RolePermission.objects.create(role=role, permission=permission)
    return role


class TestDriverListView:
    def test_anonymous_redirected_to_login(self, client):
        response = client.get(reverse("drivers:driver_list"))
        assert response.status_code == 302

    def test_user_without_permission_gets_403(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("drivers:driver_list"))
        assert response.status_code == 403

    def test_user_with_view_permission_sees_list(self, client):
        role = _role_with((Permission.Module.DRIVER, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        DriverFactory.create_batch(2)
        client.force_login(viewer)
        response = client.get(reverse("drivers:driver_list"))
        assert response.status_code == 200
        assert b"Drivers" in response.content

    def test_search_by_name(self, client):
        role = _role_with((Permission.Module.DRIVER, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        DriverFactory(first_name="Zebediah", last_name="Cross")
        client.force_login(viewer)
        response = client.get(reverse("drivers:driver_list"), {"q": "Zebediah"})
        assert response.status_code == 200
        assert b"Zebediah" in response.content

    def test_search_by_email(self, client):
        role = _role_with((Permission.Module.DRIVER, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        DriverFactory(email="findme@example.com")
        client.force_login(viewer)
        response = client.get(reverse("drivers:driver_list"), {"q": "findme@example.com"})
        assert response.status_code == 200
        assert b"findme@example.com" in response.content

    def test_status_filter(self, client):
        role = _role_with((Permission.Module.DRIVER, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        DriverFactory(employment_status=Driver.EmploymentStatus.ON_LEAVE, first_name="OnLeaveDriver")
        DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE, first_name="ActiveDriver")
        client.force_login(viewer)
        response = client.get(reverse("drivers:driver_list"), {"status": Driver.EmploymentStatus.ON_LEAVE})
        assert b"OnLeaveDriver" in response.content
        assert b"ActiveDriver" not in response.content

    def test_license_status_filter_expired(self, client):
        role = _role_with((Permission.Module.DRIVER, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        DriverFactory(
            first_name="ExpiredLicenseDriver",
            license_expiry_date=datetime.date.today() - datetime.timedelta(days=5),
        )
        DriverFactory(
            first_name="ValidLicenseDriver",
            license_expiry_date=datetime.date.today() + datetime.timedelta(days=200),
        )
        client.force_login(viewer)
        response = client.get(reverse("drivers:driver_list"), {"license_status": "EXPIRED"})
        assert b"ExpiredLicenseDriver" in response.content
        assert b"ValidLicenseDriver" not in response.content

    def test_client_filter(self, client):
        role = _role_with((Permission.Module.DRIVER, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        target_client = Client.objects.create(client_code="CLI001", client_name="Filtered Client")
        DriverFactory(first_name="ClientDriver", client=target_client)
        DriverFactory(first_name="OtherDriver")
        client.force_login(viewer)
        response = client.get(reverse("drivers:driver_list"), {"client": target_client.id})
        assert b"ClientDriver" in response.content
        assert b"OtherDriver" not in response.content

    def test_branch_filter(self, client):
        role = _role_with((Permission.Module.DRIVER, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        target_branch = Branch.objects.create(code="BR001", name="Filtered Branch")
        DriverFactory(first_name="BranchDriver", branch=target_branch)
        DriverFactory(first_name="OtherBranchDriver")
        client.force_login(viewer)
        response = client.get(reverse("drivers:driver_list"), {"branch": target_branch.id})
        assert b"BranchDriver" in response.content
        assert b"OtherBranchDriver" not in response.content

    def test_vehicle_assigned_filter(self, client):
        role = _role_with((Permission.Module.DRIVER, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        assigned_driver = DriverFactory(first_name="AssignedDriver")
        DriverFactory(first_name="UnassignedDriver")
        vehicle = VehicleFactory(current_driver=assigned_driver)
        client.force_login(viewer)
        response = client.get(reverse("drivers:driver_list"), {"vehicle": "assigned"})
        assert b"AssignedDriver" in response.content
        assert b"UnassignedDriver" not in response.content

    def test_export_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("drivers:driver_export"))
        assert response.status_code == 403

    def test_export_returns_csv(self, client):
        role = _role_with((Permission.Module.DRIVER, Permission.Action.EXPORT))
        actor = UserFactory(role=role)
        DriverFactory(first_name="ExportedDriver")
        client.force_login(actor)
        response = client.get(reverse("drivers:driver_export"))
        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv"
        assert b"ExportedDriver" in response.content


class TestDriverCreateView:
    def test_create_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("drivers:driver_create"))
        assert response.status_code == 403

    def test_create_driver_success(self, client):
        role = _role_with(
            (Permission.Module.DRIVER, Permission.Action.VIEW),
            (Permission.Module.DRIVER, Permission.Action.CREATE),
        )
        actor = UserFactory(role=role)
        client.force_login(actor)
        response = client.post(
            reverse("drivers:driver_create"),
            {
                "employee_id": "DRVNEW001",
                "first_name": "New",
                "last_name": "Driver",
                "mobile_number": "+15559990001",
                "license_number": "LICNEW001",
                "driver_type": Driver.DriverType.COMPANY,
                "employment_type": Driver.EmploymentType.FULL_TIME,
                "employment_status": Driver.EmploymentStatus.ACTIVE,
            },
        )
        assert response.status_code == 302
        assert Driver.objects.filter(employee_id="DRVNEW001").exists()

    def test_create_driver_invalid_mobile_number_rejected(self, client):
        role = _role_with(
            (Permission.Module.DRIVER, Permission.Action.VIEW),
            (Permission.Module.DRIVER, Permission.Action.CREATE),
        )
        actor = UserFactory(role=role)
        client.force_login(actor)
        response = client.post(
            reverse("drivers:driver_create"),
            {
                "employee_id": "DRVBAD001",
                "first_name": "Bad",
                "last_name": "Number",
                "mobile_number": "not-a-number",
                "license_number": "LICBAD001",
                "driver_type": Driver.DriverType.COMPANY,
                "employment_type": Driver.EmploymentType.FULL_TIME,
                "employment_status": Driver.EmploymentStatus.ACTIVE,
            },
        )
        assert response.status_code == 200
        assert not Driver.objects.filter(employee_id="DRVBAD001").exists()


class TestDriverUpdateView:
    def test_edit_updates_driver(self, client):
        role = _role_with(
            (Permission.Module.DRIVER, Permission.Action.VIEW),
            (Permission.Module.DRIVER, Permission.Action.UPDATE),
        )
        actor = UserFactory(role=role)
        driver = DriverFactory(first_name="Original")
        client.force_login(actor)
        response = client.post(
            reverse("drivers:driver_edit", kwargs={"uuid": driver.uuid}),
            {
                "employee_id": driver.employee_id,
                "first_name": "Updated",
                "last_name": driver.last_name,
                "mobile_number": driver.mobile_number,
                "license_number": driver.license_number,
                "driver_type": Driver.DriverType.COMPANY,
                "employment_type": Driver.EmploymentType.FULL_TIME,
                "employment_status": Driver.EmploymentStatus.ACTIVE,
            },
        )
        assert response.status_code == 302
        driver.refresh_from_db()
        assert driver.first_name == "Updated"


class TestDriverDetailView:
    def test_detail_requires_permission(self, client):
        actor = UserFactory(role=None)
        driver = DriverFactory()
        client.force_login(actor)
        response = client.get(reverse("drivers:driver_detail", kwargs={"uuid": driver.uuid}))
        assert response.status_code == 403

    def test_all_tabs_render(self, client):
        role = _role_with((Permission.Module.DRIVER, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        driver = DriverFactory(first_name="Ramesh", last_name="Kumar")
        client.force_login(viewer)
        for tab in ["overview", "license", "documents", "vehicles", "trips", "performance", "history"]:
            url = reverse("drivers:driver_detail", kwargs={"uuid": driver.uuid})
            response = client.get(url, {"tab": tab} if tab != "overview" else {})
            assert response.status_code == 200, f"tab={tab} failed"
        assert b"Ramesh" in response.content


class TestDriverAssignVehicle:
    def test_assign_vehicle_creates_active_assignment(self, client):
        role = _role_with(
            (Permission.Module.DRIVER, Permission.Action.VIEW),
            (Permission.Module.ASSIGNMENT, Permission.Action.CREATE),
        )
        actor = UserFactory(role=role)
        driver = DriverFactory()
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE)
        client.force_login(actor)
        response = client.post(
            reverse("drivers:driver_assign_vehicle", kwargs={"uuid": driver.uuid}),
            {
                "vehicle": vehicle.id,
                "assignment_type": VehicleDriverAssignment.AssignmentType.PRIMARY,
                "primary_driver": "on",
                "start_date": datetime.date.today().isoformat(),
                "remarks": "",
            },
        )
        assert response.status_code == 302
        vehicle.refresh_from_db()
        assert vehicle.current_driver_id == driver.id

    def test_assign_vehicle_requires_permission(self, client):
        actor = UserFactory(role=None)
        driver = DriverFactory()
        vehicle = VehicleFactory()
        client.force_login(actor)
        response = client.post(
            reverse("drivers:driver_assign_vehicle", kwargs={"uuid": driver.uuid}),
            {
                "vehicle": vehicle.id,
                "assignment_type": VehicleDriverAssignment.AssignmentType.PRIMARY,
                "primary_driver": "on",
                "start_date": datetime.date.today().isoformat(),
            },
        )
        assert response.status_code == 403

    def test_reassigning_vehicle_already_driven_by_another_driver_ends_previous(self, client):
        role = _role_with(
            (Permission.Module.DRIVER, Permission.Action.VIEW),
            (Permission.Module.ASSIGNMENT, Permission.Action.CREATE),
        )
        actor = UserFactory(role=role)
        driver1 = DriverFactory()
        driver2 = DriverFactory()
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE)
        client.force_login(actor)

        client.post(
            reverse("drivers:driver_assign_vehicle", kwargs={"uuid": driver1.uuid}),
            {
                "vehicle": vehicle.id,
                "assignment_type": VehicleDriverAssignment.AssignmentType.PRIMARY,
                "primary_driver": "on",
                "start_date": datetime.date.today().isoformat(),
            },
        )
        client.post(
            reverse("drivers:driver_assign_vehicle", kwargs={"uuid": driver2.uuid}),
            {
                "vehicle": vehicle.id,
                "assignment_type": VehicleDriverAssignment.AssignmentType.PRIMARY,
                "primary_driver": "on",
                "start_date": datetime.date.today().isoformat(),
            },
        )

        vehicle.refresh_from_db()
        assert vehicle.current_driver_id == driver2.id
        assert VehicleDriverAssignment.objects.filter(
            vehicle=vehicle, driver=driver1, status=VehicleDriverAssignment.Status.ENDED
        ).exists()

    def test_unassign_vehicle(self, client):
        role = _role_with(
            (Permission.Module.DRIVER, Permission.Action.VIEW),
            (Permission.Module.ASSIGNMENT, Permission.Action.CREATE),
            (Permission.Module.ASSIGNMENT, Permission.Action.UPDATE),
        )
        actor = UserFactory(role=role)
        driver = DriverFactory()
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE)
        client.force_login(actor)
        client.post(
            reverse("drivers:driver_assign_vehicle", kwargs={"uuid": driver.uuid}),
            {
                "vehicle": vehicle.id,
                "assignment_type": VehicleDriverAssignment.AssignmentType.PRIMARY,
                "primary_driver": "on",
                "start_date": datetime.date.today().isoformat(),
            },
        )
        assignment = VehicleDriverAssignment.objects.get(vehicle=vehicle, driver=driver)
        response = client.post(
            reverse("drivers:driver_unassign_vehicle", kwargs={"uuid": driver.uuid, "assignment_id": assignment.id})
        )
        assert response.status_code == 302
        vehicle.refresh_from_db()
        assert vehicle.current_driver_id is None


class TestDriverLifecycleActions:
    def test_deactivate_driver(self, client):
        role = _role_with(
            (Permission.Module.DRIVER, Permission.Action.VIEW),
            (Permission.Module.DRIVER, Permission.Action.ARCHIVE),
        )
        actor = UserFactory(role=role)
        driver = DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE)
        client.force_login(actor)
        response = client.post(reverse("drivers:driver_deactivate", kwargs={"uuid": driver.uuid}))
        assert response.status_code == 302
        driver.refresh_from_db()
        assert driver.employment_status == Driver.EmploymentStatus.INACTIVE

    def test_deactivate_get_not_allowed(self, client):
        role = _role_with((Permission.Module.DRIVER, Permission.Action.ARCHIVE))
        actor = UserFactory(role=role)
        driver = DriverFactory()
        client.force_login(actor)
        response = client.get(reverse("drivers:driver_deactivate", kwargs={"uuid": driver.uuid}))
        assert response.status_code == 405

    def test_activate_driver(self, client):
        role = _role_with(
            (Permission.Module.DRIVER, Permission.Action.VIEW),
            (Permission.Module.DRIVER, Permission.Action.ARCHIVE),
        )
        actor = UserFactory(role=role)
        driver = DriverFactory(employment_status=Driver.EmploymentStatus.INACTIVE)
        client.force_login(actor)
        response = client.post(reverse("drivers:driver_activate", kwargs={"uuid": driver.uuid}))
        assert response.status_code == 302
        driver.refresh_from_db()
        assert driver.employment_status == Driver.EmploymentStatus.ACTIVE
