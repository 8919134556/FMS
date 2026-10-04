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
from apps.trips import services
from apps.trips.models import Trip
from apps.trips.tests.factories import TripFactory
from apps.vehicles.models import Vehicle
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


def _role_with(*module_actions):
    role = RoleFactory()
    for module, action in module_actions:
        permission, _ = Permission.objects.get_or_create(module=module, action=action)
        RolePermission.objects.create(role=role, permission=permission)
    return role


def _full_trip_role():
    return _role_with(
        (Permission.Module.TRIP, Permission.Action.VIEW),
        (Permission.Module.TRIP, Permission.Action.CREATE),
        (Permission.Module.TRIP, Permission.Action.UPDATE),
        (Permission.Module.TRIP, Permission.Action.ARCHIVE),
        (Permission.Module.TRIP, Permission.Action.EXPORT),
    )


class TestTripListView:
    def test_anonymous_redirected_to_login(self, client):
        response = client.get(reverse("trips:trip_list"))
        assert response.status_code == 302

    def test_user_without_permission_gets_403(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("trips:trip_list"))
        assert response.status_code == 403

    def test_user_with_view_permission_sees_list(self, client):
        role = _role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        TripFactory.create_batch(2)
        client.force_login(viewer)
        response = client.get(reverse("trips:trip_list"))
        assert response.status_code == 200
        assert b"Trips" in response.content

    def test_search_by_trip_number(self, client):
        role = _role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        TripFactory(trip_number="TRP-777777")
        client.force_login(viewer)
        response = client.get(reverse("trips:trip_list"), {"q": "TRP-777777"})
        assert response.status_code == 200
        assert b"TRP-777777" in response.content

    def test_status_filter(self, client):
        role = _role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        TripFactory(trip_number="TRP-888801", status=Trip.Status.CANCELLED)
        TripFactory(trip_number="TRP-888802", status=Trip.Status.SCHEDULED)
        client.force_login(viewer)
        response = client.get(reverse("trips:trip_list"), {"status": Trip.Status.CANCELLED})
        assert b"TRP-888801" in response.content
        assert b"TRP-888802" not in response.content

    def test_client_filter(self, client):
        role = _role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        target_client = ClientFactory()
        TripFactory(trip_number="TRP-888901", client=target_client)
        TripFactory(trip_number="TRP-888902")
        client.force_login(viewer)
        response = client.get(reverse("trips:trip_list"), {"client": target_client.id})
        assert b"TRP-888901" in response.content
        assert b"TRP-888902" not in response.content

    def test_vehicle_filter(self, client):
        role = _role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        target_vehicle = VehicleFactory()
        TripFactory(trip_number="TRP-889001", vehicle=target_vehicle)
        TripFactory(trip_number="TRP-889002")
        client.force_login(viewer)
        response = client.get(reverse("trips:trip_list"), {"vehicle": target_vehicle.id})
        assert b"TRP-889001" in response.content
        assert b"TRP-889002" not in response.content

    def test_driver_filter(self, client):
        role = _role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        target_driver = DriverFactory()
        TripFactory(trip_number="TRP-889101", driver=target_driver)
        TripFactory(trip_number="TRP-889102")
        client.force_login(viewer)
        response = client.get(reverse("trips:trip_list"), {"driver": target_driver.id})
        assert b"TRP-889101" in response.content
        assert b"TRP-889102" not in response.content

    def test_status_counts_reflect_other_filters_not_the_status_itself(self, client):
        """The pill/KPI counts should be computed from every filter except
        status — so a client filter narrows every pill's count, but picking
        one status pill doesn't zero out the others."""
        role = _role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        target_client = ClientFactory()
        TripFactory(client=target_client, status=Trip.Status.SCHEDULED)
        TripFactory(client=target_client, status=Trip.Status.DELAYED)
        TripFactory(status=Trip.Status.SCHEDULED)  # different client — excluded
        client.force_login(viewer)

        response = client.get(reverse("trips:trip_list"), {"client": target_client.id, "status": "SCHEDULED"})
        assert response.status_code == 200
        counts = response.context["status_counts"]
        assert counts[Trip.Status.SCHEDULED] == 1
        assert counts[Trip.Status.DELAYED] == 1  # visible even though status=SCHEDULED is active
        assert response.context["total_trip_count"] == 2

    def test_active_kpi_groups_assigned_dispatched_in_progress(self, client):
        role = _role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        TripFactory(status=Trip.Status.ASSIGNED)
        TripFactory(status=Trip.Status.DISPATCHED)
        TripFactory(status=Trip.Status.IN_PROGRESS)
        TripFactory(status=Trip.Status.DELAYED)  # not counted as "active"
        client.force_login(viewer)
        response = client.get(reverse("trips:trip_list"))
        assert response.context["active_trip_count"] == 3

    def test_pagination(self, client):
        role = _role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        TripFactory.create_batch(30)
        client.force_login(viewer)
        response = client.get(reverse("trips:trip_list"))
        assert response.status_code == 200
        assert response.context["page_obj"].has_next()

    def test_export_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("trips:trip_export"))
        assert response.status_code == 403

    def test_export_returns_csv(self, client):
        role = _role_with((Permission.Module.TRIP, Permission.Action.EXPORT))
        actor = UserFactory(role=role)
        TripFactory(trip_number="TRP-999901")
        client.force_login(actor)
        response = client.get(reverse("trips:trip_export"))
        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv"
        assert b"TRP-999901" in response.content


class TestTripCreateView:
    def test_create_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("trips:trip_create"))
        assert response.status_code == 403

    def test_create_trip_as_scheduled(self, client):
        role = _full_trip_role()
        actor = UserFactory(role=role)
        target_client = ClientFactory()
        origin = SiteFactory(client=target_client)
        destination = SiteFactory(client=target_client)
        client.force_login(actor)
        response = client.post(
            reverse("trips:trip_create"),
            {
                "action": "schedule",
                "trip_type": Trip.TripType.DELIVERY,
                "priority": Trip.Priority.NORMAL,
                "client": target_client.id,
                "origin_site": origin.id,
                "destination_site": destination.id,
                "scheduled_start": "2030-01-01T09:00",
                "scheduled_end": "2030-01-01T13:00",
            },
        )
        assert response.status_code == 302
        trip = Trip.objects.get(client=target_client)
        assert trip.status == Trip.Status.SCHEDULED
        assert trip.trip_number.startswith("TRP-")

    def test_create_trip_as_draft(self, client):
        role = _full_trip_role()
        actor = UserFactory(role=role)
        target_client = ClientFactory()
        origin = SiteFactory(client=target_client)
        destination = SiteFactory(client=target_client)
        client.force_login(actor)
        response = client.post(
            reverse("trips:trip_create"),
            {
                "action": "draft",
                "trip_type": Trip.TripType.DELIVERY,
                "priority": Trip.Priority.NORMAL,
                "client": target_client.id,
                "origin_site": origin.id,
                "destination_site": destination.id,
                "scheduled_start": "2030-01-01T09:00",
                "scheduled_end": "2030-01-01T13:00",
            },
        )
        assert response.status_code == 302
        trip = Trip.objects.get(client=target_client)
        assert trip.status == Trip.Status.DRAFT

    def test_create_scheduled_trip_notifies_client_account_manager(self, client):
        from apps.notifications.models import Notification

        role = _full_trip_role()
        actor = UserFactory(role=role)
        account_manager = UserFactory(role=None)
        target_client = ClientFactory(account_manager=account_manager)
        origin = SiteFactory(client=target_client)
        destination = SiteFactory(client=target_client)
        client.force_login(actor)
        client.post(
            reverse("trips:trip_create"),
            {
                "action": "schedule",
                "trip_type": Trip.TripType.DELIVERY,
                "priority": Trip.Priority.NORMAL,
                "client": target_client.id,
                "origin_site": origin.id,
                "destination_site": destination.id,
                "scheduled_start": "2030-01-01T09:00",
                "scheduled_end": "2030-01-01T13:00",
            },
        )
        trip = Trip.objects.get(client=target_client)
        assert Notification.objects.filter(recipient=account_manager, link_url__icontains=str(trip.uuid)).exists()

    def test_create_draft_trip_does_not_notify_account_manager(self, client):
        from apps.notifications.models import Notification

        role = _full_trip_role()
        actor = UserFactory(role=role)
        account_manager = UserFactory(role=None)
        target_client = ClientFactory(account_manager=account_manager)
        origin = SiteFactory(client=target_client)
        destination = SiteFactory(client=target_client)
        client.force_login(actor)
        client.post(
            reverse("trips:trip_create"),
            {
                "action": "draft",
                "trip_type": Trip.TripType.DELIVERY,
                "priority": Trip.Priority.NORMAL,
                "client": target_client.id,
                "origin_site": origin.id,
                "destination_site": destination.id,
                "scheduled_start": "2030-01-01T09:00",
                "scheduled_end": "2030-01-01T13:00",
            },
        )
        assert not Notification.objects.filter(recipient=account_manager).exists()

    def test_create_trip_with_optional_route(self, client):
        from apps.routes.tests.factories import RouteFactory

        role = _full_trip_role()
        actor = UserFactory(role=role)
        target_client = ClientFactory()
        origin = SiteFactory(client=target_client)
        destination = SiteFactory(client=target_client)
        route = RouteFactory()
        client.force_login(actor)
        response = client.post(
            reverse("trips:trip_create"),
            {
                "action": "draft",
                "trip_type": Trip.TripType.DELIVERY,
                "priority": Trip.Priority.NORMAL,
                "client": target_client.id,
                "origin_site": origin.id,
                "destination_site": destination.id,
                "route": route.id,
                "scheduled_start": "2030-01-01T09:00",
                "scheduled_end": "2030-01-01T13:00",
            },
        )
        assert response.status_code == 302
        trip = Trip.objects.get(client=target_client)
        assert trip.route == route

    def test_create_trip_without_route_still_works(self, client):
        role = _full_trip_role()
        actor = UserFactory(role=role)
        target_client = ClientFactory()
        origin = SiteFactory(client=target_client)
        destination = SiteFactory(client=target_client)
        client.force_login(actor)
        response = client.post(
            reverse("trips:trip_create"),
            {
                "action": "draft",
                "trip_type": Trip.TripType.DELIVERY,
                "priority": Trip.Priority.NORMAL,
                "client": target_client.id,
                "origin_site": origin.id,
                "destination_site": destination.id,
                "scheduled_start": "2030-01-01T09:00",
                "scheduled_end": "2030-01-01T13:00",
            },
        )
        assert response.status_code == 302
        trip = Trip.objects.get(client=target_client)
        assert trip.route is None

    def test_create_rejects_mismatched_client_site(self, client):
        role = _full_trip_role()
        actor = UserFactory(role=role)
        target_client = ClientFactory()
        other_client = ClientFactory()
        origin = SiteFactory(client=other_client)
        destination = SiteFactory(client=target_client)
        client.force_login(actor)
        response = client.post(
            reverse("trips:trip_create"),
            {
                "action": "schedule",
                "trip_type": Trip.TripType.DELIVERY,
                "priority": Trip.Priority.NORMAL,
                "client": target_client.id,
                "origin_site": origin.id,
                "destination_site": destination.id,
                "scheduled_start": "2030-01-01T09:00",
                "scheduled_end": "2030-01-01T13:00",
            },
        )
        assert response.status_code == 200
        assert not Trip.objects.filter(client=target_client).exists()

    def test_create_rejects_end_before_start(self, client):
        role = _full_trip_role()
        actor = UserFactory(role=role)
        target_client = ClientFactory()
        origin = SiteFactory(client=target_client)
        destination = SiteFactory(client=target_client)
        client.force_login(actor)
        response = client.post(
            reverse("trips:trip_create"),
            {
                "action": "schedule",
                "trip_type": Trip.TripType.DELIVERY,
                "priority": Trip.Priority.NORMAL,
                "client": target_client.id,
                "origin_site": origin.id,
                "destination_site": destination.id,
                "scheduled_start": "2030-01-01T13:00",
                "scheduled_end": "2030-01-01T09:00",
            },
        )
        assert response.status_code == 200
        assert not Trip.objects.filter(client=target_client).exists()


class TestTripDetailView:
    def test_detail_requires_permission(self, client):
        actor = UserFactory(role=None)
        trip = TripFactory()
        client.force_login(actor)
        response = client.get(reverse("trips:trip_detail", kwargs={"uuid": trip.uuid}))
        assert response.status_code == 403

    def test_all_tabs_render(self, client):
        role = _role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        trip = TripFactory(trip_number="TRP-TABTEST")
        client.force_login(viewer)
        for tab in ["overview", "route", "tracking", "documents", "expenses", "activity"]:
            url = reverse("trips:trip_detail", kwargs={"uuid": trip.uuid})
            response = client.get(url, {"tab": tab} if tab != "overview" else {})
            assert response.status_code == 200, f"tab={tab} failed"


class TestTripWorkflowViews:
    def test_schedule_draft(self, client):
        role = _full_trip_role()
        actor = UserFactory(role=role)
        trip = TripFactory(status=Trip.Status.DRAFT)
        client.force_login(actor)
        response = client.post(reverse("trips:trip_schedule", kwargs={"uuid": trip.uuid}))
        assert response.status_code == 302
        trip.refresh_from_db()
        assert trip.status == Trip.Status.SCHEDULED

    def test_assign_view_success(self, client):
        role = _full_trip_role()
        actor = UserFactory(role=role)
        trip = TripFactory(status=Trip.Status.SCHEDULED)
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE, client=trip.client)
        driver = DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE, client=trip.client)
        client.force_login(actor)
        response = client.post(
            reverse("trips:trip_assign", kwargs={"uuid": trip.uuid}),
            {"vehicle": vehicle.id, "driver": driver.id},
        )
        assert response.status_code == 302
        trip.refresh_from_db()
        assert trip.status == Trip.Status.ASSIGNED

    def test_assign_requires_permission(self, client):
        actor = UserFactory(role=None)
        trip = TripFactory(status=Trip.Status.SCHEDULED)
        client.force_login(actor)
        response = client.post(reverse("trips:trip_assign", kwargs={"uuid": trip.uuid}), {})
        assert response.status_code == 403

    def test_dispatch_view(self, client):
        role = _full_trip_role()
        actor = UserFactory(role=role)
        trip = TripFactory(status=Trip.Status.SCHEDULED)
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE, client=trip.client)
        driver = DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE, client=trip.client)
        services.assign_trip(trip=trip, vehicle=vehicle, driver=driver, assigned_by=actor)
        client.force_login(actor)
        response = client.post(reverse("trips:trip_dispatch", kwargs={"uuid": trip.uuid}))
        assert response.status_code == 302
        trip.refresh_from_db()
        assert trip.status == Trip.Status.DISPATCHED

    def test_start_view(self, client):
        role = _full_trip_role()
        actor = UserFactory(role=role)
        trip = TripFactory(status=Trip.Status.SCHEDULED)
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE, client=trip.client)
        driver = DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE, client=trip.client)
        services.assign_trip(trip=trip, vehicle=vehicle, driver=driver, assigned_by=actor)
        services.dispatch_trip(trip=trip, dispatched_by=actor)
        client.force_login(actor)
        response = client.post(reverse("trips:trip_start", kwargs={"uuid": trip.uuid}))
        assert response.status_code == 302
        trip.refresh_from_db()
        assert trip.status == Trip.Status.IN_PROGRESS

    def test_delay_view(self, client):
        role = _full_trip_role()
        actor = UserFactory(role=role)
        trip = TripFactory(status=Trip.Status.IN_PROGRESS)
        client.force_login(actor)
        response = client.post(
            reverse("trips:trip_delay", kwargs={"uuid": trip.uuid}),
            {"delay_reason": Trip.DelayReason.TRAFFIC, "delay_notes": "Heavy traffic"},
        )
        assert response.status_code == 302
        trip.refresh_from_db()
        assert trip.status == Trip.Status.DELAYED

    def test_resume_view(self, client):
        role = _full_trip_role()
        actor = UserFactory(role=role)
        trip = TripFactory(status=Trip.Status.DELAYED)
        client.force_login(actor)
        response = client.post(reverse("trips:trip_resume", kwargs={"uuid": trip.uuid}))
        assert response.status_code == 302
        trip.refresh_from_db()
        assert trip.status == Trip.Status.IN_PROGRESS

    def test_complete_view(self, client):
        role = _full_trip_role()
        actor = UserFactory(role=role)
        trip = TripFactory(status=Trip.Status.IN_PROGRESS)
        client.force_login(actor)
        response = client.post(
            reverse("trips:trip_complete", kwargs={"uuid": trip.uuid}),
            {
                "actual_end": timezone.now().strftime("%Y-%m-%dT%H:%M"),
                "actual_distance": "150.5",
                "fuel_used": "12.0",
                "driver_remarks": "All good",
                "completion_notes": "Delivered on time",
            },
        )
        assert response.status_code == 302
        trip.refresh_from_db()
        assert trip.status == Trip.Status.COMPLETED

    def test_cancel_requires_reason(self, client):
        role = _full_trip_role()
        actor = UserFactory(role=role)
        trip = TripFactory(status=Trip.Status.SCHEDULED)
        client.force_login(actor)
        response = client.post(reverse("trips:trip_cancel", kwargs={"uuid": trip.uuid}), {"cancellation_reason": ""})
        assert response.status_code == 302
        trip.refresh_from_db()
        assert trip.status == Trip.Status.SCHEDULED

    def test_cancel_success(self, client):
        role = _full_trip_role()
        actor = UserFactory(role=role)
        trip = TripFactory(status=Trip.Status.SCHEDULED)
        client.force_login(actor)
        response = client.post(
            reverse("trips:trip_cancel", kwargs={"uuid": trip.uuid}),
            {"cancellation_reason": "Client changed plans"},
        )
        assert response.status_code == 302
        trip.refresh_from_db()
        assert trip.status == Trip.Status.CANCELLED

    def test_cancel_requires_archive_permission(self, client):
        role = _role_with(
            (Permission.Module.TRIP, Permission.Action.VIEW), (Permission.Module.TRIP, Permission.Action.UPDATE)
        )
        actor = UserFactory(role=role)
        trip = TripFactory(status=Trip.Status.SCHEDULED)
        client.force_login(actor)
        response = client.post(
            reverse("trips:trip_cancel", kwargs={"uuid": trip.uuid}),
            {"cancellation_reason": "test"},
        )
        assert response.status_code == 403
