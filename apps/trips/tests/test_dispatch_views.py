import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Permission, RolePermission
from apps.accounts.tests.factories import RoleFactory, UserFactory
from apps.audit.models import AuditLog
from apps.drivers.models import Driver
from apps.drivers.tests.factories import DriverFactory
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


def _view_only_role():
    return _role_with((Permission.Module.TRIP, Permission.Action.VIEW))


def _full_trip_role():
    return _role_with(
        (Permission.Module.TRIP, Permission.Action.VIEW),
        (Permission.Module.TRIP, Permission.Action.UPDATE),
    )


BOARD_URL = "dispatch:board"


class TestDispatchBoardAccess:
    def test_anonymous_redirected_to_login(self, client):
        response = client.get(reverse(BOARD_URL))
        assert response.status_code == 302

    def test_missing_view_permission_gets_403(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL))
        assert response.status_code == 403

    def test_view_permission_sees_board(self, client):
        viewer = UserFactory(role=_view_only_role())
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL))
        assert response.status_code == 200
        assert b"Dispatch Board" in response.content

    def test_view_only_hides_action_buttons(self, client):
        viewer = UserFactory(role=_view_only_role())
        TripFactory(status=Trip.Status.SCHEDULED, scheduled_start=timezone.now())
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL))
        assert response.context["can_act"] is False
        assert b"Assign Resources" not in response.content

    def test_action_endpoint_still_blocked_without_update_permission(self, client):
        actor = UserFactory(role=_view_only_role())
        trip = TripFactory(status=Trip.Status.SCHEDULED)
        client.force_login(actor)
        response = client.post(reverse("trips:trip_assign", kwargs={"uuid": trip.uuid}), {})
        assert response.status_code == 403


class TestDispatchBoardGrouping:
    def test_each_status_lands_in_its_column(self, client):
        viewer = UserFactory(role=_view_only_role())
        today = timezone.now()
        trips = {
            "needs_assignment": TripFactory(status=Trip.Status.SCHEDULED, scheduled_start=today),
            "ready": TripFactory(status=Trip.Status.ASSIGNED, scheduled_start=today),
            "dispatched": TripFactory(status=Trip.Status.DISPATCHED, scheduled_start=today),
            "in_progress": TripFactory(status=Trip.Status.IN_PROGRESS, scheduled_start=today),
            "delayed": TripFactory(status=Trip.Status.DELAYED, scheduled_start=today),
        }
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL))
        by_key = {c["key"]: c for c in response.context["columns"]}
        for key, trip in trips.items():
            assert [t.pk for t in by_key[key]["trips"]] == [trip.pk]

    def test_completed_and_cancelled_excluded(self, client):
        viewer = UserFactory(role=_view_only_role())
        today = timezone.now()
        TripFactory(status=Trip.Status.COMPLETED, scheduled_start=today)
        TripFactory(status=Trip.Status.CANCELLED, scheduled_start=today)
        TripFactory(status=Trip.Status.DRAFT, scheduled_start=today)
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL))
        total = sum(c["count"] for c in response.context["columns"])
        assert total == 0


class TestDispatchBoardDateNav:
    def test_default_shows_only_today(self, client):
        viewer = UserFactory(role=_view_only_role())
        today_trip = TripFactory(status=Trip.Status.SCHEDULED, scheduled_start=timezone.now())
        TripFactory(status=Trip.Status.SCHEDULED, scheduled_start=timezone.now() - datetime.timedelta(days=1))
        TripFactory(status=Trip.Status.SCHEDULED, scheduled_start=timezone.now() + datetime.timedelta(days=1))
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL))
        total_trips = [t for c in response.context["columns"] for t in c["trips"]]
        assert [t.pk for t in total_trips] == [today_trip.pk]

    def test_date_param_targets_that_day(self, client):
        viewer = UserFactory(role=_view_only_role())
        target_date = timezone.now() + datetime.timedelta(days=2)
        trip = TripFactory(status=Trip.Status.SCHEDULED, scheduled_start=target_date)
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL), {"date": target_date.date().isoformat()})
        total_trips = [t for c in response.context["columns"] for t in c["trips"]]
        assert [t.pk for t in total_trips] == [trip.pk]

    def test_prev_next_are_adjacent_dates(self, client):
        viewer = UserFactory(role=_view_only_role())
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL), {"date": "2030-06-15"})
        assert response.context["date_prev"] == "2030-06-14"
        assert response.context["date_next"] == "2030-06-16"


class TestDispatchBoardQuickFilters:
    def _make_board(self):
        today = timezone.now()
        return {
            "needs_assignment": TripFactory(status=Trip.Status.SCHEDULED, scheduled_start=today),
            "ready": TripFactory(status=Trip.Status.ASSIGNED, scheduled_start=today),
            "in_progress": TripFactory(status=Trip.Status.IN_PROGRESS, scheduled_start=today),
            "delayed": TripFactory(status=Trip.Status.DELAYED, scheduled_start=today),
        }

    @pytest.mark.parametrize("quick_filter,key", [
        ("needs_assignment", "needs_assignment"),
        ("ready", "ready"),
        ("in_progress", "in_progress"),
        ("delayed", "delayed"),
    ])
    def test_quick_filter_isolates_status(self, client, quick_filter, key):
        viewer = UserFactory(role=_view_only_role())
        trips = self._make_board()
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL), {"quick_filter": quick_filter})
        visible = [t.pk for c in response.context["columns"] for t in c["trips"]]
        assert visible == [trips[key].pk]

    def test_past_due_quick_filter(self, client):
        viewer = UserFactory(role=_view_only_role())
        past_due = TripFactory(
            status=Trip.Status.SCHEDULED,
            scheduled_start=timezone.now() - datetime.timedelta(hours=1),
            scheduled_end=timezone.now() + datetime.timedelta(hours=1),
        )
        TripFactory(
            status=Trip.Status.SCHEDULED,
            scheduled_start=timezone.now() + datetime.timedelta(hours=1),
            scheduled_end=timezone.now() + datetime.timedelta(hours=3),
        )
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL), {"quick_filter": "past_due"})
        visible = [t.pk for c in response.context["columns"] for t in c["trips"]]
        assert visible == [past_due.pk]

    def test_all_shows_everything(self, client):
        viewer = UserFactory(role=_view_only_role())
        self._make_board()
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL))
        total = sum(c["count"] for c in response.context["columns"])
        assert total == 4


class TestDispatchBoardRegularFilters:
    def test_client_filter(self, client):
        from apps.clients.tests.factories import ClientFactory

        viewer = UserFactory(role=_view_only_role())
        target_client = ClientFactory()
        today = timezone.now()
        matching = TripFactory(status=Trip.Status.SCHEDULED, client=target_client, scheduled_start=today)
        TripFactory(status=Trip.Status.SCHEDULED, scheduled_start=today)
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL), {"client": target_client.id})
        visible = [t.pk for c in response.context["columns"] for t in c["trips"]]
        assert visible == [matching.pk]

    def test_priority_filter(self, client):
        viewer = UserFactory(role=_view_only_role())
        today = timezone.now()
        urgent = TripFactory(status=Trip.Status.SCHEDULED, priority=Trip.Priority.URGENT, scheduled_start=today)
        TripFactory(status=Trip.Status.SCHEDULED, priority=Trip.Priority.LOW, scheduled_start=today)
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL), {"priority": Trip.Priority.URGENT})
        visible = [t.pk for c in response.context["columns"] for t in c["trips"]]
        assert visible == [urgent.pk]

    def test_driver_filter(self, client):
        viewer = UserFactory(role=_view_only_role())
        driver = DriverFactory()
        today = timezone.now()
        matching = TripFactory(status=Trip.Status.ASSIGNED, driver=driver, scheduled_start=today)
        TripFactory(status=Trip.Status.ASSIGNED, scheduled_start=today)
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL), {"driver": driver.id})
        visible = [t.pk for c in response.context["columns"] for t in c["trips"]]
        assert visible == [matching.pk]

    def test_start_time_range_filter(self, client):
        viewer = UserFactory(role=_view_only_role())
        today = timezone.now().replace(hour=10, minute=0, second=0, microsecond=0)
        morning = TripFactory(status=Trip.Status.SCHEDULED, scheduled_start=today)
        TripFactory(status=Trip.Status.SCHEDULED, scheduled_start=today.replace(hour=20))
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL), {"start_time_from": "08:00", "start_time_to": "12:00"})
        visible = [t.pk for c in response.context["columns"] for t in c["trips"]]
        assert visible == [morning.pk]

    def test_search_filter_matches_trip_number(self, client):
        viewer = UserFactory(role=_view_only_role())
        today = timezone.now()
        matching = TripFactory(status=Trip.Status.SCHEDULED, trip_number="TRP-770001", scheduled_start=today)
        TripFactory(status=Trip.Status.SCHEDULED, trip_number="TRP-770002", scheduled_start=today)
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL), {"q": "770001"})
        visible = [t.pk for c in response.context["columns"] for t in c["trips"]]
        assert visible == [matching.pk]

    def test_trip_type_filter(self, client):
        viewer = UserFactory(role=_view_only_role())
        today = timezone.now()
        pickup = TripFactory(status=Trip.Status.SCHEDULED, trip_type=Trip.TripType.PICKUP, scheduled_start=today)
        TripFactory(status=Trip.Status.SCHEDULED, trip_type=Trip.TripType.DELIVERY, scheduled_start=today)
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL), {"trip_type": Trip.TripType.PICKUP})
        visible = [t.pk for c in response.context["columns"] for t in c["trips"]]
        assert visible == [pickup.pk]


class TestDispatchBoardQuickFilterCounts:
    def test_counts_reflect_other_filters_not_quick_filter_itself(self, client):
        viewer = UserFactory(role=_view_only_role())
        today = timezone.now()
        TripFactory(status=Trip.Status.SCHEDULED, scheduled_start=today)
        TripFactory(status=Trip.Status.DELAYED, scheduled_start=today)
        client.force_login(viewer)

        response = client.get(reverse(BOARD_URL), {"quick_filter": "needs_assignment"})
        counts = response.context["quick_filter_counts"]
        assert counts["needs_assignment"] == 1
        assert counts["delayed"] == 1  # visible even though a different pill is active
        assert counts["all"] == 2

    def test_past_due_count(self, client):
        viewer = UserFactory(role=_view_only_role())
        today = timezone.now()
        TripFactory(status=Trip.Status.SCHEDULED, scheduled_start=today - datetime.timedelta(hours=2))
        TripFactory(status=Trip.Status.SCHEDULED, scheduled_start=today + datetime.timedelta(hours=2))
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL))
        assert response.context["quick_filter_counts"]["past_due"] == 1


class TestDispatchBoardAttentionSummary:
    def test_needs_assignment_and_delayed_counts_match_columns(self, client):
        viewer = UserFactory(role=_view_only_role())
        today = timezone.now()
        TripFactory.create_batch(2, status=Trip.Status.SCHEDULED, scheduled_start=today)
        TripFactory(status=Trip.Status.DELAYED, scheduled_start=today)
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL))
        summary = {item["key"]: item["count"] for item in response.context["attention_summary"]}
        assert summary["needs_assignment"] == 2
        assert summary["delayed"] == 1

    def test_summary_stays_stable_when_a_quick_filter_pill_is_active(self, client):
        """The exact mismatched-counts bug the redesign brief calls out:
        the attention tiles must not collapse to 0 just because a different
        quick-filter pill is currently selected — that's the pill bar's job,
        not the summary strip's."""
        viewer = UserFactory(role=_view_only_role())
        today = timezone.now()
        TripFactory.create_batch(2, status=Trip.Status.SCHEDULED, scheduled_start=today)
        TripFactory(status=Trip.Status.DELAYED, scheduled_start=today)
        client.force_login(viewer)

        response = client.get(reverse(BOARD_URL), {"quick_filter": "needs_assignment"})
        summary = {item["key"]: item["count"] for item in response.context["attention_summary"]}
        assert summary["needs_assignment"] == 2
        assert summary["delayed"] == 1  # not zeroed out by the active "needs_assignment" pill

    def test_conflicts_count_matches_conflict_category_alerts(self, client):
        viewer = UserFactory(role=_view_only_role())
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL))
        summary = {item["key"]: item["count"] for item in response.context["attention_summary"]}
        conflict_alerts = [a for a in response.context["alerts"] if a.get("category") == "conflict"]
        assert summary["conflicts"] == len(conflict_alerts)


class TestDispatchBoardAvailability:
    def test_available_vehicle_listed(self, client):
        viewer = UserFactory(role=_view_only_role())
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE, availability_status=Vehicle.AvailabilityStatus.AVAILABLE)
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL))
        assert vehicle in response.context["available_vehicles"]

    def test_on_trip_vehicle_excluded(self, client):
        viewer = UserFactory(role=_view_only_role())
        VehicleFactory(status=Vehicle.Status.ACTIVE, availability_status=Vehicle.AvailabilityStatus.ON_TRIP)
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL))
        assert response.context["available_vehicles"] == []

    def test_available_driver_listed(self, client):
        viewer = UserFactory(role=_view_only_role())
        driver = DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE)
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL))
        assert driver in response.context["available_drivers"]

    def test_expired_license_driver_excluded(self, client):
        viewer = UserFactory(role=_view_only_role())
        DriverFactory(
            employment_status=Driver.EmploymentStatus.ACTIVE,
            license_expiry_date=timezone.now().date() - datetime.timedelta(days=1),
        )
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL))
        assert response.context["available_drivers"] == []


class TestDispatchBoardActionRoundTrip:
    def test_assign_redirects_to_next_not_trip_detail(self, client):
        actor = UserFactory(role=_full_trip_role())
        trip = TripFactory(status=Trip.Status.SCHEDULED)
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE, client=trip.client)
        driver = DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE, client=trip.client)
        next_url = "/dispatch/?date=2030-01-01"
        client.force_login(actor)
        response = client.post(
            reverse("trips:trip_assign", kwargs={"uuid": trip.uuid}),
            {"vehicle": vehicle.id, "driver": driver.id, "next": next_url},
        )
        assert response.status_code == 302
        assert response.url == next_url
        trip.refresh_from_db()
        assert trip.status == Trip.Status.ASSIGNED
        assert AuditLog.objects.filter(module="trip", entity="Trip", action=AuditLog.Action.ASSIGN).exists()

    def test_invalid_assignment_redirects_back_with_error_and_no_change(self, client):
        actor = UserFactory(role=_full_trip_role())
        trip = TripFactory(status=Trip.Status.SCHEDULED)
        vehicle = VehicleFactory(status=Vehicle.Status.INACTIVE, client=trip.client)
        driver = DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE, client=trip.client)
        next_url = "/dispatch/"
        client.force_login(actor)
        response = client.post(
            reverse("trips:trip_assign", kwargs={"uuid": trip.uuid}),
            {"vehicle": vehicle.id, "driver": driver.id, "next": next_url},
        )
        assert response.status_code == 302
        assert response.url == next_url
        trip.refresh_from_db()
        assert trip.status == Trip.Status.SCHEDULED

    def test_dispatch_start_delay_resume_complete_all_honor_next(self, client):
        actor = UserFactory(role=_full_trip_role())
        trip = TripFactory(status=Trip.Status.SCHEDULED)
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE, client=trip.client)
        driver = DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE, client=trip.client)
        services.assign_trip(trip=trip, vehicle=vehicle, driver=driver, assigned_by=actor)
        next_url = "/dispatch/?quick_filter=in_progress"
        client.force_login(actor)

        response = client.post(reverse("trips:trip_dispatch", kwargs={"uuid": trip.uuid}), {"next": next_url})
        assert response.url == next_url

        response = client.post(reverse("trips:trip_start", kwargs={"uuid": trip.uuid}), {"next": next_url})
        assert response.url == next_url

        response = client.post(
            reverse("trips:trip_delay", kwargs={"uuid": trip.uuid}),
            {"delay_reason": Trip.DelayReason.TRAFFIC, "delay_notes": "Traffic", "next": next_url},
        )
        assert response.url == next_url

        response = client.post(reverse("trips:trip_resume", kwargs={"uuid": trip.uuid}), {"next": next_url})
        assert response.url == next_url

        response = client.post(
            reverse("trips:trip_complete", kwargs={"uuid": trip.uuid}),
            {"actual_end": timezone.now().strftime("%Y-%m-%dT%H:%M"), "next": next_url},
        )
        assert response.url == next_url
        trip.refresh_from_db()
        assert trip.status == Trip.Status.COMPLETED

    def test_unsafe_next_falls_back_to_trip_detail(self, client):
        actor = UserFactory(role=_full_trip_role())
        trip = TripFactory(status=Trip.Status.SCHEDULED)
        client.force_login(actor)
        response = client.post(
            reverse("trips:trip_schedule", kwargs={"uuid": trip.uuid}),
            {"next": "https://evil.example/"},
        )
        assert response.status_code == 302
        assert response.url == reverse("trips:trip_detail", kwargs={"uuid": trip.uuid})

    def test_no_next_falls_back_to_trip_detail(self, client):
        actor = UserFactory(role=_full_trip_role())
        trip = TripFactory(status=Trip.Status.DRAFT)
        client.force_login(actor)
        response = client.post(reverse("trips:trip_schedule", kwargs={"uuid": trip.uuid}))
        assert response.url == reverse("trips:trip_detail", kwargs={"uuid": trip.uuid})


class TestDispatchBoardAlerts:
    def test_alert_url_resolves(self, client):
        viewer = UserFactory(role=_view_only_role())
        TripFactory(status=Trip.Status.DELAYED)
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL))
        assert response.context["alerts"], "expected at least one alert"
        for alert in response.context["alerts"]:
            alert_response = client.get(alert["url"])
            assert alert_response.status_code == 200

    def test_no_alerts_empty_state(self, client):
        viewer = UserFactory(role=_view_only_role())
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL))
        assert response.context["alerts"] == []
        assert b"All clear" in response.content


class TestDispatchBoardEmptyStates:
    def test_no_trips_today(self, client):
        viewer = UserFactory(role=_view_only_role())
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL))
        assert b"No trips waiting for assignment." in response.content
        assert b"No delayed trips." in response.content

    def test_no_available_vehicles(self, client):
        viewer = UserFactory(role=_view_only_role())
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL))
        assert b"No vehicles available" in response.content

    def test_no_available_drivers(self, client):
        viewer = UserFactory(role=_view_only_role())
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL))
        assert b"No drivers available" in response.content

    def test_no_recent_activity(self, client):
        viewer = UserFactory(role=_view_only_role())
        client.force_login(viewer)
        response = client.get(reverse(BOARD_URL))
        assert b"No dispatch activity recorded yet." in response.content


class TestDispatchBoardQueryEfficiency:
    def test_query_count_does_not_grow_with_board_size(self, client):
        """First query-count assertion in this repo (via Django's
        CaptureQueriesContext, which — unlike connection.queries — captures
        regardless of settings.DEBUG). Proves group_into_columns/
        available_vehicles/available_drivers/dispatch_alerts don't add a
        query per row (no N+1) as the board grows."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        viewer = UserFactory(role=_view_only_role())
        client.force_login(viewer)
        today = timezone.now()
        TripFactory(status=Trip.Status.SCHEDULED, scheduled_start=today)
        VehicleFactory(status=Vehicle.Status.ACTIVE, availability_status=Vehicle.AvailabilityStatus.AVAILABLE)
        DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE)

        with CaptureQueriesContext(connection) as small:
            response = client.get(reverse(BOARD_URL))
        assert response.status_code == 200

        for _ in range(20):
            TripFactory(status=Trip.Status.SCHEDULED, scheduled_start=today)
            VehicleFactory(status=Vehicle.Status.ACTIVE, availability_status=Vehicle.AvailabilityStatus.AVAILABLE)
            DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE)

        with CaptureQueriesContext(connection) as large:
            response = client.get(reverse(BOARD_URL))
        assert response.status_code == 200

        assert len(large.captured_queries) == len(small.captured_queries)
