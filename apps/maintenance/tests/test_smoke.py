"""End-to-end smoke test for the Maintenance module: create -> start ->
complete, all 6 detail tabs, Vehicle integration (tab + service fields),
Trip conflict rules (both directions), and Dashboard KPIs — mirrors the
smoke tests for Vehicles/Drivers/Clients/Sites/Trips, the quality
benchmark this module was built to match.
"""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Permission, RolePermission
from apps.accounts.tests.factories import RoleFactory, UserFactory
from apps.drivers.models import Driver
from apps.drivers.tests.factories import DriverFactory
from apps.maintenance.models import Maintenance
from apps.trips.models import Trip
from apps.trips.tests.factories import TripFactory
from apps.vehicles.models import Vehicle
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


def _full_access_role():
    role = RoleFactory()
    for module in [
        Permission.Module.MAINTENANCE, Permission.Module.VEHICLE, Permission.Module.TRIP, Permission.Module.DRIVER,
        Permission.Module.DOCUMENT, Permission.Module.AUDIT_LOG,
    ]:
        for action in [
            Permission.Action.VIEW, Permission.Action.CREATE, Permission.Action.UPDATE,
            Permission.Action.ARCHIVE, Permission.Action.EXPORT,
        ]:
            permission, _ = Permission.objects.get_or_create(module=module, action=action)
            RolePermission.objects.create(role=role, permission=permission)
    return role


class TestMaintenanceModuleSmoke:
    def test_full_lifecycle_all_tabs_and_vehicle_integration(self, client):
        role = _full_access_role()
        actor = UserFactory(role=role)
        client.force_login(actor)

        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE, odometer_reading=10000)

        create_response = client.post(
            reverse("maintenance:maintenance_create"),
            {
                "vehicle": vehicle.id,
                "maintenance_type": Maintenance.MaintenanceType.PREVENTIVE,
                "priority": Maintenance.Priority.HIGH,
                "scheduled_date": timezone.now().date().isoformat(),
                "odometer_at_service": "10000",
                "service_center": "Main Street Garage",
                "parts-TOTAL_FORMS": "1",
                "parts-INITIAL_FORMS": "0",
                "parts-MIN_NUM_FORMS": "0",
                "parts-MAX_NUM_FORMS": "1000",
                "parts-0-part_name": "Oil Filter",
                "parts-0-part_number": "OF-01",
                "parts-0-quantity": "1",
                "parts-0-unit_cost": "20.00",
            },
        )
        assert create_response.status_code == 302
        maintenance = Maintenance.objects.get(vehicle=vehicle)
        assert maintenance.status == Maintenance.Status.SCHEDULED

        edit_response = client.get(reverse("maintenance:maintenance_edit", kwargs={"uuid": maintenance.uuid}))
        assert edit_response.status_code == 200

        start_response = client.post(reverse("maintenance:maintenance_start", kwargs={"uuid": maintenance.uuid}))
        assert start_response.status_code == 302
        maintenance.refresh_from_db()
        vehicle.refresh_from_db()
        assert maintenance.status == Maintenance.Status.IN_PROGRESS
        assert vehicle.status == Vehicle.Status.UNDER_MAINTENANCE

        # Vehicle undergoing maintenance must not be assignable to a new trip.
        trip = TripFactory(status=Trip.Status.SCHEDULED)
        driver = DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE)
        blocked_assign_response = client.post(
            reverse("trips:trip_assign", kwargs={"uuid": trip.uuid}),
            {"vehicle": vehicle.id, "driver": driver.id},
        )
        assert blocked_assign_response.status_code == 302
        trip.refresh_from_db()
        assert trip.status == Trip.Status.SCHEDULED  # unchanged — assignment was rejected

        complete_response = client.post(
            reverse("maintenance:maintenance_complete", kwargs={"uuid": maintenance.uuid}),
            {
                "completed_date": timezone.now().strftime("%Y-%m-%dT%H:%M"),
                "final_odometer": "10250",
                "actual_cost": "3500",
                "labor_cost": "1000",
                "work_performed": "Full preventive service completed",
                "completion_notes": "No issues found",
                "next_service_date": (timezone.now().date() + datetime.timedelta(days=90)).isoformat(),
                "next_service_odometer": "20000",
            },
        )
        assert complete_response.status_code == 302
        maintenance.refresh_from_db()
        vehicle.refresh_from_db()
        assert maintenance.status == Maintenance.Status.COMPLETED
        assert vehicle.status == Vehicle.Status.ACTIVE
        assert vehicle.last_service_odometer == 10250
        assert vehicle.odometer_reading == 10250
        assert vehicle.next_service_odometer == 20000

        for tab in ["overview", "service", "parts", "costs", "documents", "history"]:
            url = reverse("maintenance:maintenance_detail", kwargs={"uuid": maintenance.uuid})
            response = client.get(url, {"tab": tab} if tab != "overview" else {})
            assert response.status_code == 200, f"tab={tab} failed"

        parts_response = client.get(reverse("maintenance:maintenance_detail", kwargs={"uuid": maintenance.uuid}), {"tab": "parts"})
        assert b"Oil Filter" in parts_response.content

        # Now that maintenance completed and the vehicle is active again, the
        # trip assignment that was blocked earlier must succeed.
        now_allowed_response = client.post(
            reverse("trips:trip_assign", kwargs={"uuid": trip.uuid}),
            {"vehicle": vehicle.id, "driver": driver.id},
        )
        assert now_allowed_response.status_code == 302
        trip.refresh_from_db()
        assert trip.status == Trip.Status.ASSIGNED

        # Cross-module integration: real maintenance history on the vehicle's own tab.
        vehicle_maintenance_response = client.get(
            reverse("vehicles:vehicle_detail", kwargs={"uuid": vehicle.uuid}), {"tab": "maintenance"}
        )
        assert vehicle_maintenance_response.status_code == 200
        assert maintenance.maintenance_number.encode() in vehicle_maintenance_response.content

        list_response = client.get(reverse("maintenance:maintenance_list"))
        assert list_response.status_code == 200

        export_response = client.get(reverse("maintenance:maintenance_export"))
        assert export_response.status_code == 200
        assert export_response["Content-Type"] == "text/csv"

        dashboard_response = client.get(reverse("core:dashboard"))
        assert dashboard_response.status_code == 200
        assert b"Maintenance Scheduled" in dashboard_response.content
        assert b"Maintenance Overdue" in dashboard_response.content

    def test_cannot_start_maintenance_on_vehicle_with_active_trip(self, client):
        role = _full_access_role()
        actor = UserFactory(role=role)
        client.force_login(actor)

        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE)
        TripFactory(vehicle=vehicle, status=Trip.Status.IN_PROGRESS)

        create_response = client.post(
            reverse("maintenance:maintenance_create"),
            {
                "vehicle": vehicle.id,
                "maintenance_type": Maintenance.MaintenanceType.REPAIR,
                "priority": Maintenance.Priority.CRITICAL,
                "scheduled_date": timezone.now().date().isoformat(),
                "odometer_at_service": "5000",
                "parts-TOTAL_FORMS": "0",
                "parts-INITIAL_FORMS": "0",
                "parts-MIN_NUM_FORMS": "0",
                "parts-MAX_NUM_FORMS": "1000",
            },
        )
        assert create_response.status_code == 302
        maintenance = Maintenance.objects.get(vehicle=vehicle)

        start_response = client.post(reverse("maintenance:maintenance_start", kwargs={"uuid": maintenance.uuid}))
        assert start_response.status_code == 302
        maintenance.refresh_from_db()
        assert maintenance.status == Maintenance.Status.SCHEDULED  # blocked — vehicle has an active trip

    def test_dashboard_maintenance_due_widget_and_kpis_with_no_records(self, client):
        role = _full_access_role()
        actor = UserFactory(role=role)
        client.force_login(actor)
        response = client.get(reverse("core:dashboard"))
        assert response.status_code == 200
        assert b"No maintenance issues require attention" in response.content
