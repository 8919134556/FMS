import datetime

import pytest
from django.utils import timezone

from apps.alerts import services
from apps.alerts.models import Alert
from apps.documents.tests.factories import DocumentFactory
from apps.maintenance.tests.factories import MaintenanceFactory
from apps.trips.models import Trip
from apps.trips.tests.factories import TripFactory

pytestmark = pytest.mark.django_db


class TestSyncAlerts:
    def test_delayed_trip_creates_an_open_alert(self):
        trip = TripFactory(status=Trip.Status.DELAYED)
        services.sync_alerts()
        alert = Alert.objects.get(dedupe_key=f"TRIP_DELAYED:{trip.pk}")
        assert alert.status == Alert.Status.OPEN
        assert alert.category == Alert.Category.TRIP_DELAYED
        assert alert.content_object == trip

    def test_scheduled_unassigned_trip_creates_alert(self):
        trip = TripFactory(status=Trip.Status.SCHEDULED)
        services.sync_alerts()
        assert Alert.objects.filter(dedupe_key=f"TRIP_UNASSIGNED:{trip.pk}").exists()

    def test_overdue_maintenance_creates_critical_alert(self):
        maintenance = MaintenanceFactory(scheduled_date=timezone.now().date() - datetime.timedelta(days=2))
        services.sync_alerts()
        alert = Alert.objects.get(dedupe_key=f"MAINTENANCE_OVERDUE:{maintenance.pk}")
        assert alert.severity == Alert.Severity.CRITICAL

    def test_expired_document_creates_alert(self):
        document = DocumentFactory(expiry_date=timezone.now().date() - datetime.timedelta(days=1))
        services.sync_alerts()
        assert Alert.objects.filter(dedupe_key=f"DOCUMENT_EXPIRED:{document.pk}").exists()

    def test_expiring_document_creates_alert(self):
        document = DocumentFactory(expiry_date=timezone.now().date() + datetime.timedelta(days=5))
        services.sync_alerts()
        assert Alert.objects.filter(dedupe_key=f"DOCUMENT_EXPIRING:{document.pk}").exists()

    def test_resync_does_not_duplicate_alerts(self):
        TripFactory(status=Trip.Status.DELAYED)
        services.sync_alerts()
        services.sync_alerts()
        assert Alert.objects.filter(category=Alert.Category.TRIP_DELAYED).count() == 1

    def test_condition_clearing_auto_resolves_the_alert(self):
        trip = TripFactory(status=Trip.Status.DELAYED)
        services.sync_alerts()
        alert = Alert.objects.get(dedupe_key=f"TRIP_DELAYED:{trip.pk}")
        assert alert.status == Alert.Status.OPEN

        trip.status = Trip.Status.COMPLETED
        trip.save(update_fields=["status"])
        services.sync_alerts()
        alert.refresh_from_db()
        assert alert.status == Alert.Status.RESOLVED

    def test_condition_reappearing_reopens_a_resolved_alert(self):
        trip = TripFactory(status=Trip.Status.DELAYED)
        services.sync_alerts()
        trip.status = Trip.Status.COMPLETED
        trip.save(update_fields=["status"])
        services.sync_alerts()

        trip.status = Trip.Status.DELAYED
        trip.save(update_fields=["status"])
        services.sync_alerts()
        alert = Alert.objects.get(dedupe_key=f"TRIP_DELAYED:{trip.pk}")
        assert alert.status == Alert.Status.OPEN

    def test_acknowledged_alert_stays_acknowledged_while_condition_persists(self):
        trip = TripFactory(status=Trip.Status.DELAYED)
        services.sync_alerts()
        alert = Alert.objects.get(dedupe_key=f"TRIP_DELAYED:{trip.pk}")
        alert.status = Alert.Status.ACKNOWLEDGED
        alert.save(update_fields=["status"])

        services.sync_alerts()
        alert.refresh_from_db()
        assert alert.status == Alert.Status.ACKNOWLEDGED
