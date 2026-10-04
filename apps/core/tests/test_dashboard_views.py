import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Permission
from apps.accounts.tests.factories import UserFactory
from apps.core.tests.factories import role_with
from apps.drivers.tests.factories import DriverFactory
from apps.maintenance.models import Maintenance
from apps.maintenance.tests.factories import MaintenanceFactory
from apps.trips.models import Trip
from apps.trips.tests.factories import TripFactory
from apps.vehicles.models import Vehicle
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


def _full_dashboard_role():
    return role_with(
        (Permission.Module.VEHICLE, Permission.Action.VIEW),
        (Permission.Module.DRIVER, Permission.Action.VIEW),
        (Permission.Module.CLIENT, Permission.Action.VIEW),
        (Permission.Module.SITE, Permission.Action.VIEW),
        (Permission.Module.TRIP, Permission.Action.VIEW),
        (Permission.Module.MAINTENANCE, Permission.Action.VIEW),
        (Permission.Module.DOCUMENT, Permission.Action.VIEW),
    )


class TestDashboardAccess:
    def test_anonymous_redirected_to_login(self, client):
        response = client.get(reverse("core:dashboard"))
        assert response.status_code == 302

    def test_authenticated_user_sees_dashboard(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert response.status_code == 200
        assert b"Fleet Operations" in response.content


class TestPermissionAwareWidgets:
    def test_no_trip_permission_hides_trip_widgets(self, client):
        viewer = UserFactory(role=None)
        TripFactory()
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert response.status_code == 200
        assert b"Today's Operations" not in response.content
        assert b"Live Operational Board" not in response.content
        assert b"Total Trips" not in response.content

    def test_trip_permission_shows_trip_widgets(self, client):
        role = role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        TripFactory()
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert b"Total Trips" in response.content

    def test_no_maintenance_permission_hides_maintenance_widgets(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert b"Maintenance Overview" not in response.content

    def test_maintenance_permission_shows_maintenance_widgets(self, client):
        role = role_with((Permission.Module.MAINTENANCE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert b"Maintenance Overview" in response.content
        assert b"Maintenance Scheduled" in response.content

    def test_no_document_permission_hides_document_widgets(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert b"Document Compliance" not in response.content

    def test_document_permission_shows_document_widgets(self, client):
        role = role_with((Permission.Module.DOCUMENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert b"Document Compliance" in response.content
        assert b"Total Documents" in response.content

    def test_no_vehicle_permission_hides_vehicle_kpis(self, client):
        viewer = UserFactory(role=None)
        VehicleFactory()
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert b"Total Vehicles" not in response.content

    def test_no_driver_permission_hides_driver_availability(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert b"Driver Availability" not in response.content

    def test_no_client_permission_hides_client_operations(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert b"Client Operations" not in response.content

    def test_no_site_permission_hides_site_operations(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert b"Site Operations" not in response.content

    def test_permission_combination_shows_only_permitted_sections(self, client):
        role = role_with((Permission.Module.VEHICLE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert b"Total Vehicles" in response.content
        assert b"Maintenance Overview" not in response.content
        assert b"Document Compliance" not in response.content


class TestFleetKPIs:
    def test_real_vehicle_counts(self, client):
        role = role_with((Permission.Module.VEHICLE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        VehicleFactory(status=Vehicle.Status.ACTIVE, availability_status=Vehicle.AvailabilityStatus.AVAILABLE)
        VehicleFactory(status=Vehicle.Status.ACTIVE, availability_status=Vehicle.AvailabilityStatus.ON_TRIP)
        VehicleFactory(status=Vehicle.Status.UNDER_MAINTENANCE, availability_status=Vehicle.AvailabilityStatus.MAINTENANCE)
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert response.status_code == 200
        assert response.context["total_vehicles"] == 3
        assert response.context["available_vehicles"] == 1
        assert response.context["vehicles_on_trip"] == 1


class TestTodaysOperations:
    def test_todays_trips_shown(self, client):
        role = role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        TripFactory(status=Trip.Status.SCHEDULED, scheduled_start=timezone.now())
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert response.context["trip_status_summary"]["scheduled"] == 1

    def test_empty_state_no_trips_today(self, client):
        role = role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert b"No trips scheduled" in response.content


class TestDelayedAndUnassignedTrips:
    def test_delayed_trip_appears_in_attention(self, client):
        role = role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        TripFactory(status=Trip.Status.DELAYED)
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert b"Delayed Trip" in response.content

    def test_unassigned_scheduled_trip_appears_in_attention(self, client):
        role = role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        TripFactory(status=Trip.Status.SCHEDULED)
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert b"Unassigned Trip" in response.content

    def test_delayed_trip_kpi_links_to_dispatch_board(self, client):
        role = role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        TripFactory(status=Trip.Status.DELAYED)
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert b"/dispatch/?quick_filter=delayed" in response.content

    def test_unassigned_trip_attention_item_links_to_dispatch_board(self, client):
        role = role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        TripFactory(status=Trip.Status.SCHEDULED)
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        needs_assignment_item = next(
            item for item in response.context["attention_items"] if "Unassigned Trip" in item["title"]
        )
        assert needs_assignment_item["url"] == "/dispatch/?quick_filter=needs_assignment"


class TestMaintenanceAlerts:
    def test_overdue_maintenance_in_attention_and_overview(self, client):
        role = role_with((Permission.Module.MAINTENANCE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        MaintenanceFactory(scheduled_date=timezone.now().date() - datetime.timedelta(days=3))
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert b"Overdue Maintenance" in response.content
        assert response.context["maintenance_overview"]["overdue"] == 1

    def test_empty_state_no_maintenance_issues(self, client):
        role = role_with((Permission.Module.MAINTENANCE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert b"No maintenance issues require attention" in response.content


class TestDocumentExpiryAlerts:
    def test_expiring_document_in_compliance_widget(self, client):
        from apps.documents.tests.factories import DocumentFactory

        role = role_with((Permission.Module.DOCUMENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        DocumentFactory(title="Fleet Insurance Doc", expiry_date=timezone.now().date() + datetime.timedelta(days=3))
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert b"Fleet Insurance Doc" in response.content

    def test_empty_state_no_expiring_documents(self, client):
        role = role_with((Permission.Module.DOCUMENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert b"No documents are expiring soon" in response.content


class TestQuickActions:
    def test_quick_action_hidden_without_permission(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert b"Create Trip" not in response.content

    def test_quick_action_shown_with_permission(self, client):
        role = role_with((Permission.Module.TRIP, Permission.Action.CREATE))
        viewer = UserFactory(role=role)
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert b"Create Trip" in response.content


class TestDateRangeFilter:
    def test_default_date_range_is_today(self, client):
        role = role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert response.context["date_range"] == "today"

    def test_date_range_param_respected(self, client):
        role = role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"), {"date_range": "week"})
        assert response.context["date_range"] == "week"
        assert response.context["date_range_label"] == "This Week"

    def test_invalid_date_range_falls_back_to_today(self, client):
        role = role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"), {"date_range": "bogus"})
        assert response.context["date_range"] == "today"

    def test_date_range_does_not_affect_realtime_fleet_status(self, client):
        """Vehicle current-state counts must be identical regardless of the
        date-range selector — they are not date-scoped."""
        role = role_with((Permission.Module.VEHICLE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        VehicleFactory(availability_status=Vehicle.AvailabilityStatus.ON_TRIP)
        client.force_login(viewer)
        today_response = client.get(reverse("core:dashboard"), {"date_range": "today"})
        month_response = client.get(reverse("core:dashboard"), {"date_range": "month"})
        assert today_response.context["vehicles_on_trip"] == month_response.context["vehicles_on_trip"] == 1


class TestWidgetNavigation:
    def test_maintenance_overview_links_to_maintenance_detail(self, client):
        role = role_with((Permission.Module.MAINTENANCE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        m = MaintenanceFactory(scheduled_date=timezone.now().date() - datetime.timedelta(days=1))
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert reverse("maintenance:maintenance_detail", kwargs={"uuid": m.uuid}).encode() in response.content

    def test_document_compliance_links_to_document_detail(self, client):
        from apps.documents.tests.factories import DocumentFactory

        role = role_with((Permission.Module.DOCUMENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        doc = DocumentFactory(expiry_date=timezone.now().date() + datetime.timedelta(days=2))
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert reverse("documents:document_detail", kwargs={"uuid": doc.uuid}).encode() in response.content

    def test_recent_trips_links_to_trip_detail(self, client):
        role = role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        trip = TripFactory()
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert reverse("trips:trip_detail", kwargs={"uuid": trip.uuid}).encode() in response.content


class TestRecentTripsFix:
    """The previously-reported bug: Recent Trips was a stale locked
    placeholder claiming "Once the Trips module ships in Phase 6...". """

    def test_no_stale_locked_placeholder_text(self, client):
        role = role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        # The old placeholder text specifically — not a blanket "Phase 6"
        # check, since the sidebar legitimately shows other still-locked
        # modules (e.g. Routes) tagged "Arrives in Phase 6".
        assert b"Once the Trips module ships" not in response.content
        assert b"Recent Trips arrives in Phase" not in response.content

    def test_real_trip_shown_in_recent_trips(self, client):
        role = role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        trip = TripFactory()
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert trip.trip_number.encode() in response.content


class TestDashboardQueryPerformance:
    def test_dashboard_does_not_produce_excessive_queries(self, client, django_assert_max_num_queries):
        role = _full_dashboard_role()
        viewer = UserFactory(role=role)

        for _ in range(8):
            vehicle = VehicleFactory()
            driver = DriverFactory()
            TripFactory(vehicle=vehicle, driver=driver)
            MaintenanceFactory(vehicle=vehicle)

        client.force_login(viewer)
        # Generous ceiling — this asserts the dashboard doesn't scale
        # linearly with row count (no per-row query loop), not an exact
        # budget that would be brittle across incidental future tweaks.
        with django_assert_max_num_queries(80):
            response = client.get(reverse("core:dashboard"))
        assert response.status_code == 200


class TestFleetConnectivityWidget:
    def test_widget_shows_real_counts_for_authorized_viewer(self, client):
        from apps.tracking.tests.factories import VehicleCurrentTelemetryFactory

        role = role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        VehicleCurrentTelemetryFactory(vehicle=VehicleFactory(), timestamp=timezone.now())
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert response.status_code == 200
        assert response.context["fleet_connectivity"]["online"] == 1
        assert b"Fleet Connectivity" in response.content
        assert b"/tracking/live/?status=online" in response.content

    def test_widget_absent_without_tracking_device_permission(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert response.status_code == 200
        assert response.context["fleet_connectivity"] is None
        assert b"Fleet Connectivity" not in response.content

    def test_gps_dead_variables_no_longer_referenced(self, client):
        """Phase 3.4 replaced the unused device-status-based gps_* context
        keys with real telemetry-based fleet_connectivity counts."""
        role = role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        client.force_login(viewer)
        response = client.get(reverse("core:dashboard"))
        assert "gps_online" not in response.context
        assert "gps_offline" not in response.context
        assert "gps_no_device" not in response.context
