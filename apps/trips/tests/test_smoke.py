"""End-to-end smoke test for the Trips module: create -> assign -> dispatch
-> start -> delay -> resume -> complete, all 6 detail tabs, and the
integration points into Vehicles/Drivers/Clients/Sites/Dashboard — mirrors
the smoke tests for Vehicles/Drivers/Clients/Sites, which are the quality
benchmark this module was built to match.
"""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Permission, RolePermission
from apps.accounts.tests.factories import RoleFactory, UserFactory
from apps.clients.tests.factories import ClientFactory
from apps.drivers.models import Driver
from apps.drivers.tests.factories import DriverFactory
from apps.locations.tests.factories import SiteFactory
from apps.trips.models import Trip
from apps.vehicles.models import Vehicle
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


def _full_access_role():
    role = RoleFactory()
    for module in [
        Permission.Module.TRIP, Permission.Module.VEHICLE, Permission.Module.DRIVER,
        Permission.Module.CLIENT, Permission.Module.SITE,
    ]:
        for action in [
            Permission.Action.VIEW, Permission.Action.CREATE, Permission.Action.UPDATE,
            Permission.Action.ARCHIVE, Permission.Action.EXPORT,
        ]:
            permission, _ = Permission.objects.get_or_create(module=module, action=action)
            RolePermission.objects.create(role=role, permission=permission)
    return role


class TestTripModuleSmoke:
    def test_full_trip_lifecycle_all_tabs_and_cross_module_integration(self, client):
        role = _full_access_role()
        actor = UserFactory(role=role)
        client.force_login(actor)

        trip_client = ClientFactory(client_name="Smoke Freight Co")
        origin = SiteFactory(client=trip_client, site_name="Origin Warehouse")
        destination = SiteFactory(client=trip_client, site_name="Destination Depot")
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE, client=trip_client)
        driver = DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE, client=trip_client)

        create_response = client.post(
            reverse("trips:trip_create"),
            {
                "action": "schedule",
                "trip_type": Trip.TripType.DELIVERY,
                "priority": Trip.Priority.HIGH,
                "client": trip_client.id,
                "origin_site": origin.id,
                "destination_site": destination.id,
                "scheduled_start": "2030-06-01T09:00",
                "scheduled_end": "2030-06-01T15:00",
                "planned_distance": "250",
            },
        )
        assert create_response.status_code == 302
        trip = Trip.objects.get(client=trip_client)
        assert trip.status == Trip.Status.SCHEDULED

        assign_response = client.post(
            reverse("trips:trip_assign", kwargs={"uuid": trip.uuid}),
            {"vehicle": vehicle.id, "driver": driver.id},
        )
        assert assign_response.status_code == 302
        trip.refresh_from_db()
        assert trip.status == Trip.Status.ASSIGNED

        assert client.post(reverse("trips:trip_dispatch", kwargs={"uuid": trip.uuid})).status_code == 302
        trip.refresh_from_db()
        assert trip.status == Trip.Status.DISPATCHED
        vehicle.refresh_from_db()
        assert vehicle.availability_status == Vehicle.AvailabilityStatus.ON_TRIP

        assert client.post(reverse("trips:trip_start", kwargs={"uuid": trip.uuid})).status_code == 302
        trip.refresh_from_db()
        assert trip.status == Trip.Status.IN_PROGRESS

        delay_response = client.post(
            reverse("trips:trip_delay", kwargs={"uuid": trip.uuid}),
            {"delay_reason": Trip.DelayReason.TRAFFIC, "delay_notes": "Jam near the ring road"},
        )
        assert delay_response.status_code == 302
        trip.refresh_from_db()
        assert trip.status == Trip.Status.DELAYED

        assert client.post(reverse("trips:trip_resume", kwargs={"uuid": trip.uuid})).status_code == 302
        trip.refresh_from_db()
        assert trip.status == Trip.Status.IN_PROGRESS

        complete_response = client.post(
            reverse("trips:trip_complete", kwargs={"uuid": trip.uuid}),
            {
                "actual_end": timezone.now().strftime("%Y-%m-%dT%H:%M"),
                "actual_distance": "260",
                "fuel_used": "20",
                "driver_remarks": "No issues after the delay",
                "completion_notes": "Delivered successfully",
            },
        )
        assert complete_response.status_code == 302
        trip.refresh_from_db()
        assert trip.status == Trip.Status.COMPLETED
        vehicle.refresh_from_db()
        assert vehicle.availability_status != Vehicle.AvailabilityStatus.ON_TRIP

        for tab in ["overview", "route", "tracking", "documents", "expenses", "activity"]:
            url = reverse("trips:trip_detail", kwargs={"uuid": trip.uuid})
            response = client.get(url, {"tab": tab} if tab != "overview" else {})
            assert response.status_code == 200, f"tab={tab} failed"

        activity_response = client.get(reverse("trips:trip_detail", kwargs={"uuid": trip.uuid}), {"tab": "activity"})
        assert b"Trip" in activity_response.content

        # Cross-module integration: real trip data must surface on every
        # related entity's Trips tab, not just the trip's own detail page.
        vehicle_trips_response = client.get(
            reverse("vehicles:vehicle_detail", kwargs={"uuid": vehicle.uuid}), {"tab": "trips"}
        )
        assert vehicle_trips_response.status_code == 200
        assert trip.trip_number.encode() in vehicle_trips_response.content

        driver_trips_response = client.get(
            reverse("drivers:driver_detail", kwargs={"uuid": driver.uuid}), {"tab": "trips"}
        )
        assert driver_trips_response.status_code == 200
        assert trip.trip_number.encode() in driver_trips_response.content

        driver_performance_response = client.get(
            reverse("drivers:driver_detail", kwargs={"uuid": driver.uuid}), {"tab": "performance"}
        )
        assert driver_performance_response.status_code == 200
        assert b"Completed" in driver_performance_response.content

        client_trips_response = client.get(
            reverse("clients:client_detail", kwargs={"uuid": trip_client.uuid}), {"tab": "trips"}
        )
        assert client_trips_response.status_code == 200
        assert trip.trip_number.encode() in client_trips_response.content

        site_trips_response = client.get(
            reverse("locations:site_detail", kwargs={"uuid": origin.uuid}), {"tab": "trips"}
        )
        assert site_trips_response.status_code == 200
        assert trip.trip_number.encode() in site_trips_response.content

        dashboard_response = client.get(reverse("core:dashboard"))
        assert dashboard_response.status_code == 200
        assert b"Total Trips" in dashboard_response.content
        assert b"Completed Trips" in dashboard_response.content

    def test_cancel_workflow_and_conflict_detection(self, client):
        role = _full_access_role()
        actor = UserFactory(role=role)
        client.force_login(actor)

        trip_client = ClientFactory()
        origin = SiteFactory(client=trip_client)
        destination = SiteFactory(client=trip_client)
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE, client=trip_client)
        driver = DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE, client=trip_client)

        def _create_trip(start, end):
            response = client.post(
                reverse("trips:trip_create"),
                {
                    "action": "schedule",
                    "trip_type": Trip.TripType.DELIVERY,
                    "priority": Trip.Priority.NORMAL,
                    "client": trip_client.id,
                    "origin_site": origin.id,
                    "destination_site": destination.id,
                    "scheduled_start": start,
                    "scheduled_end": end,
                },
            )
            assert response.status_code == 302
            return Trip.objects.order_by("-created_at").first()

        trip_a = _create_trip("2031-01-01T09:00", "2031-01-01T13:00")
        trip_b = _create_trip("2031-01-01T12:00", "2031-01-01T16:00")

        assert client.post(
            reverse("trips:trip_assign", kwargs={"uuid": trip_a.uuid}),
            {"vehicle": vehicle.id, "driver": driver.id},
        ).status_code == 302
        trip_a.refresh_from_db()
        assert trip_a.status == Trip.Status.ASSIGNED

        # trip_b overlaps trip_a's window on the same vehicle+driver — must be blocked.
        conflict_response = client.post(
            reverse("trips:trip_assign", kwargs={"uuid": trip_b.uuid}),
            {"vehicle": vehicle.id, "driver": driver.id},
        )
        assert conflict_response.status_code == 302
        trip_b.refresh_from_db()
        assert trip_b.status == Trip.Status.SCHEDULED  # unchanged — assignment was rejected

        cancel_response = client.post(
            reverse("trips:trip_cancel", kwargs={"uuid": trip_a.uuid}),
            {"cancellation_reason": "Client requested cancellation"},
        )
        assert cancel_response.status_code == 302
        trip_a.refresh_from_db()
        assert trip_a.status == Trip.Status.CANCELLED
        assert trip_a.cancellation_reason == "Client requested cancellation"
