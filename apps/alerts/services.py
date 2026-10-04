"""Turns real operational conditions into persisted Alert rows.

No Celery/scheduler — ``sync_alerts()`` is called synchronously whenever the
Alerts list is viewed (apps.alerts.views.AlertListView) and is also exposed
as ``python manage.py sync_alerts`` for anyone who wants to run it from cron/
Task Scheduler. Each category mirrors a definition apps.core.dashboard_services
already established (same delayed/unassigned/overdue/expiring querysets), so
the two panels can never disagree about what counts as delayed or overdue.
"""

from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from django.utils import timezone

from apps.alerts.models import Alert
from apps.core.models import SystemSettings
from apps.documents.models import Document
from apps.maintenance.models import Maintenance
from apps.trips.models import Trip

# Categories this sync pass owns end-to-end: any OPEN/ACKNOWLEDGED alert in
# one of these categories whose underlying condition no longer holds gets
# auto-resolved. A category left out of this set is never auto-resolved
# (not currently used, but keeps hand-managed categories safe by default).
_MANAGED_CATEGORIES = {
    Alert.Category.TRIP_DELAYED,
    Alert.Category.TRIP_UNASSIGNED,
    Alert.Category.MAINTENANCE_OVERDUE,
    Alert.Category.DOCUMENT_EXPIRED,
    Alert.Category.DOCUMENT_EXPIRING,
}


def _upsert(seen_keys, dedupe_key, content_object, **defaults):
    seen_keys.add(dedupe_key)
    content_type = ContentType.objects.get_for_model(content_object) if content_object else None
    object_id = content_object.pk if content_object else None
    alert, created = Alert.objects.get_or_create(
        dedupe_key=dedupe_key,
        defaults={"content_type": content_type, "object_id": object_id, **defaults},
    )
    if not created and alert.status == Alert.Status.RESOLVED:
        # Condition re-appeared after being resolved (e.g. a trip goes
        # delayed again) — reopen it instead of silently staying resolved.
        alert.status = Alert.Status.OPEN
        alert.resolved_by = None
        alert.resolved_at = None
        alert.title = defaults.get("title", alert.title)
        alert.message = defaults.get("message", alert.message)
        alert.save(update_fields=["status", "resolved_by", "resolved_at", "title", "message", "updated_at"])
    return alert


def sync_alerts():
    """Reconciles Alert rows against current data. Returns the number of
    categories synced (for the management command's output)."""
    seen_keys = set()
    settings_obj = SystemSettings.load()

    for trip in Trip.objects.filter(status=Trip.Status.DELAYED).select_related("client"):
        _upsert(
            seen_keys, f"TRIP_DELAYED:{trip.pk}", trip,
            category=Alert.Category.TRIP_DELAYED, severity=Alert.Severity.HIGH,
            title=f"Trip {trip.trip_number} is delayed",
            message=f"Client: {trip.client.client_name}",
            link_url=reverse("trips:trip_detail", kwargs={"uuid": trip.uuid}),
        )

    for trip in Trip.objects.filter(status=Trip.Status.SCHEDULED).select_related("client"):
        _upsert(
            seen_keys, f"TRIP_UNASSIGNED:{trip.pk}", trip,
            category=Alert.Category.TRIP_UNASSIGNED, severity=Alert.Severity.MEDIUM,
            title=f"Trip {trip.trip_number} needs a vehicle and driver",
            message=f"Client: {trip.client.client_name}",
            link_url=reverse("dispatch:board") + "?quick_filter=needs_assignment",
        )

    for maintenance in Maintenance.objects.overdue().select_related("vehicle"):
        _upsert(
            seen_keys, f"MAINTENANCE_OVERDUE:{maintenance.pk}", maintenance,
            category=Alert.Category.MAINTENANCE_OVERDUE, severity=Alert.Severity.CRITICAL,
            title=f"Maintenance overdue for {maintenance.vehicle.registration_number}",
            message=maintenance.get_maintenance_type_display(),
            link_url=reverse("maintenance:maintenance_detail", kwargs={"uuid": maintenance.uuid}),
        )

    for document in Document.objects.expired().select_related("content_type"):
        _upsert(
            seen_keys, f"DOCUMENT_EXPIRED:{document.pk}", document,
            category=Alert.Category.DOCUMENT_EXPIRED, severity=Alert.Severity.HIGH,
            title=f"{document.get_document_type_display()} has expired",
            message=str(document.content_object) if document.content_object else "",
            link_url=reverse("documents:document_list") + "?expiry=expired",
        )

    for document in Document.objects.expiring_soon(days=settings_obj.document_expiry_warning_days).select_related("content_type"):
        _upsert(
            seen_keys, f"DOCUMENT_EXPIRING:{document.pk}", document,
            category=Alert.Category.DOCUMENT_EXPIRING, severity=Alert.Severity.MEDIUM,
            title=f"{document.get_document_type_display()} is expiring soon",
            message=str(document.content_object) if document.content_object else "",
            link_url=reverse("documents:document_list") + "?expiry=expiring_soon",
        )

    resolved = (
        Alert.objects.filter(category__in=_MANAGED_CATEGORIES, status__in=[Alert.Status.OPEN, Alert.Status.ACKNOWLEDGED])
        .exclude(dedupe_key__in=seen_keys)
        .update(status=Alert.Status.RESOLVED, resolved_at=timezone.now())
    )
    return {"categories_synced": len(_MANAGED_CATEGORIES), "seen": len(seen_keys), "auto_resolved": resolved}
