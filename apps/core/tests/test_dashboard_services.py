import datetime

import pytest
from django.utils import timezone

from apps.accounts.models import Permission
from apps.accounts.tests.factories import UserFactory
from apps.clients.tests.factories import ClientFactory
from apps.core.dashboard_services import DashboardService, OperationsService
from apps.core.tests.factories import role_with
from apps.drivers.models import Driver
from apps.drivers.tests.factories import DriverFactory
from apps.locations.tests.factories import SiteFactory
from apps.maintenance.models import Maintenance
from apps.maintenance.tests.factories import MaintenanceFactory
from apps.trips.models import Trip
from apps.trips.tests.factories import TripFactory
from apps.vehicles.models import Vehicle
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


class TestOperationsServicePermissions:
    def test_fleet_status_none_without_vehicle_permission(self):
        viewer = UserFactory(role=None)
        assert OperationsService(viewer).fleet_status() is None

    def test_fleet_status_returned_with_permission(self):
        role = role_with((Permission.Module.VEHICLE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        VehicleFactory(status=Vehicle.Status.ACTIVE, availability_status=Vehicle.AvailabilityStatus.AVAILABLE)
        result = OperationsService(viewer).fleet_status()
        assert result is not None
        assert result["total"] == 1
        assert result["available"] == 1

    def test_driver_availability_none_without_permission(self):
        viewer = UserFactory(role=None)
        assert OperationsService(viewer).driver_availability() is None

    def test_maintenance_overview_none_without_permission(self):
        viewer = UserFactory(role=None)
        assert OperationsService(viewer).maintenance_overview() is None

    def test_document_compliance_none_without_permission(self):
        viewer = UserFactory(role=None)
        assert OperationsService(viewer).document_compliance() is None

    def test_client_operations_none_without_permission(self):
        viewer = UserFactory(role=None)
        assert OperationsService(viewer).client_operations() is None


class TestFleetStatus:
    def test_counts_by_availability_status(self):
        role = role_with((Permission.Module.VEHICLE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        VehicleFactory(availability_status=Vehicle.AvailabilityStatus.AVAILABLE)
        VehicleFactory(availability_status=Vehicle.AvailabilityStatus.ON_TRIP)
        VehicleFactory(availability_status=Vehicle.AvailabilityStatus.MAINTENANCE)
        VehicleFactory(status=Vehicle.Status.INACTIVE)

        result = OperationsService(viewer).fleet_status()
        assert result["total"] == 4
        assert result["available"] == 1
        assert result["on_trip"] == 1
        assert result["maintenance"] == 1
        assert result["inactive"] == 1


class TestDriverAvailability:
    def test_counts_available_assigned_on_trip_inactive(self):
        role = role_with(
            (Permission.Module.DRIVER, Permission.Action.VIEW), (Permission.Module.TRIP, Permission.Action.VIEW)
        )
        viewer = UserFactory(role=role)

        DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE)  # available
        DriverFactory(employment_status=Driver.EmploymentStatus.INACTIVE)  # inactive

        on_trip_driver = DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE)
        TripFactory(driver=on_trip_driver, status=Trip.Status.IN_PROGRESS)

        assigned_driver = DriverFactory(employment_status=Driver.EmploymentStatus.ACTIVE)
        VehicleFactory(current_driver=assigned_driver)

        result = OperationsService(viewer).driver_availability()
        assert result["total"] == 4
        assert result["on_trip"] == 1
        assert result["assigned"] == 1
        assert result["available"] == 1
        assert result["inactive"] == 1
        assert len(result["table_rows"]) == 2  # on-trip + assigned, not the fully-available one


class TestMaintenanceOverview:
    def test_reuses_maintenance_queryset_methods(self):
        role = role_with((Permission.Module.MAINTENANCE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        MaintenanceFactory(scheduled_date=timezone.now().date() - datetime.timedelta(days=2))  # overdue
        MaintenanceFactory(scheduled_date=timezone.now().date())  # due today
        MaintenanceFactory(status=Maintenance.Status.IN_PROGRESS)

        result = OperationsService(viewer).maintenance_overview()
        assert result["overdue"] == 1
        assert result["due_today"] == 1
        assert result["in_progress"] == 1


class TestDocumentCompliance:
    def test_reuses_document_queryset_methods(self):
        from apps.documents.models import Document
        from apps.documents.tests.factories import DocumentFactory

        role = role_with((Permission.Module.DOCUMENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        DocumentFactory(expiry_date=timezone.now().date() - datetime.timedelta(days=1))  # expired
        DocumentFactory(expiry_date=timezone.now().date() + datetime.timedelta(days=5))  # expiring in 7
        DocumentFactory(expiry_date=timezone.now().date() + datetime.timedelta(days=20))  # expiring in 30

        result = OperationsService(viewer).document_compliance()
        assert result["expired"] == 1
        assert result["expiring_7_days"] == 1
        assert result["expiring_30_days"] == 2  # both the 5-day and 20-day ones fall within 30


class TestClientOperations:
    def test_counts_active_and_trip_states(self):
        role = role_with(
            (Permission.Module.CLIENT, Permission.Action.VIEW), (Permission.Module.TRIP, Permission.Action.VIEW)
        )
        viewer = UserFactory(role=role)
        active_with_trip = ClientFactory(status="ACTIVE")
        TripFactory(client=active_with_trip, status=Trip.Status.IN_PROGRESS)
        delayed_client = ClientFactory(status="ACTIVE")
        TripFactory(client=delayed_client, status=Trip.Status.DELAYED)
        ClientFactory(status="ACTIVE")

        result = OperationsService(viewer).client_operations()
        assert result["active_clients"] == 3
        assert result["with_active_trips"] == 1
        assert result["with_delayed_trips"] == 1

    def test_maintenance_issues_omitted_without_maintenance_permission(self):
        role = role_with((Permission.Module.CLIENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        result = OperationsService(viewer).client_operations()
        assert result["with_maintenance_issues"] == 0


class TestAttentionItems:
    def test_delayed_trips_alert_requires_trip_permission(self):
        viewer_without = UserFactory(role=None)
        TripFactory(status=Trip.Status.DELAYED)
        items = OperationsService(viewer_without).attention_items()
        assert not any("Delayed" in item["title"] for item in items)

        role = role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer_with = UserFactory(role=role)
        items = OperationsService(viewer_with).attention_items()
        assert any("Delayed" in item["title"] for item in items)

    def test_overdue_maintenance_alert_requires_maintenance_permission(self):
        MaintenanceFactory(scheduled_date=timezone.now().date() - datetime.timedelta(days=1))
        role = role_with((Permission.Module.MAINTENANCE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        items = OperationsService(viewer).attention_items()
        assert any("Overdue Maintenance" in item["title"] for item in items)

    def test_expired_documents_alert_requires_document_permission(self):
        from apps.documents.tests.factories import DocumentFactory

        DocumentFactory(expiry_date=timezone.now().date() - datetime.timedelta(days=1))
        role = role_with((Permission.Module.DOCUMENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        items = OperationsService(viewer).attention_items()
        assert any("Expired Document" in item["title"] for item in items)

    def test_empty_when_nothing_to_report(self):
        role = role_with(
            (Permission.Module.TRIP, Permission.Action.VIEW), (Permission.Module.MAINTENANCE, Permission.Action.VIEW)
        )
        viewer = UserFactory(role=role)
        assert OperationsService(viewer).attention_items() == []

    def test_sorted_by_severity(self):
        MaintenanceFactory(scheduled_date=timezone.now().date() - datetime.timedelta(days=1))  # critical
        TripFactory(status=Trip.Status.SCHEDULED)  # medium (unassigned)
        role = role_with(
            (Permission.Module.TRIP, Permission.Action.VIEW), (Permission.Module.MAINTENANCE, Permission.Action.VIEW)
        )
        viewer = UserFactory(role=role)
        items = OperationsService(viewer).attention_items()
        severities = [item["severity"] for item in items]
        assert severities == sorted(severities, key=lambda s: {"critical": 0, "high": 1, "medium": 2, "low": 3}[s])


class TestDashboardServiceDateRange:
    def test_defaults_to_today(self):
        role = role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        service = DashboardService(viewer)
        assert service.date_range == "today"
        assert service.start == service.end == timezone.now().date()

    def test_invalid_range_falls_back_to_today(self):
        role = role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        service = DashboardService(viewer, date_range="not-a-real-range")
        assert service.date_range == "today"

    def test_yesterday_range(self):
        role = role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        service = DashboardService(viewer, date_range="yesterday")
        expected = timezone.now().date() - datetime.timedelta(days=1)
        assert service.start == service.end == expected

    def test_month_range_starts_on_first_of_month(self):
        role = role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        service = DashboardService(viewer, date_range="month")
        assert service.start == timezone.now().date().replace(day=1)
        assert service.end == timezone.now().date()

    def test_trip_status_summary_scoped_to_range(self):
        role = role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        today_trip = TripFactory(status=Trip.Status.SCHEDULED, scheduled_start=timezone.now())
        TripFactory(
            status=Trip.Status.SCHEDULED,
            scheduled_start=timezone.now() + datetime.timedelta(days=10),
        )
        summary = DashboardService(viewer, date_range="today").trip_status_summary()
        assert summary["scheduled"] == 1
        assert summary["total"] == 1

    def test_none_without_trip_permission(self):
        viewer = UserFactory(role=None)
        service = DashboardService(viewer)
        assert service.trip_status_summary() is None
        assert service.active_trips_board() is None
        assert service.recent_trips() is None


class TestRecentTrips:
    def test_not_scoped_to_date_range(self):
        """Recent Trips must show real recent activity regardless of the
        date-range selector — this was the dashboard's stale placeholder
        bug; the fix must not accidentally become date-range-scoped too."""
        role = role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        old_trip = TripFactory(scheduled_start=timezone.now() - datetime.timedelta(days=60))
        recent = DashboardService(viewer, date_range="today").recent_trips()
        assert old_trip in recent


class TestSiteOperations:
    def test_counts_outbound_and_inbound_trips(self):
        role = role_with(
            (Permission.Module.SITE, Permission.Action.VIEW), (Permission.Module.TRIP, Permission.Action.VIEW)
        )
        viewer = UserFactory(role=role)
        site_a = SiteFactory()
        site_b = SiteFactory()
        today_start = timezone.now()
        TripFactory(origin_site=site_a, destination_site=site_b, scheduled_start=today_start)
        TripFactory(origin_site=site_b, destination_site=site_a, scheduled_start=today_start)

        results = {s.id: s for s in DashboardService(viewer, date_range="today").site_operations()}
        assert results[site_a.id].outbound == 1
        assert results[site_a.id].inbound == 1
        assert results[site_a.id].trips_total == 2

    def test_none_without_site_permission(self):
        role = role_with((Permission.Module.TRIP, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        assert DashboardService(viewer).site_operations() is None

    def test_none_without_trip_permission(self):
        role = role_with((Permission.Module.SITE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        assert DashboardService(viewer).site_operations() is None
