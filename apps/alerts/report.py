"""Alert Report: the vehicle alert EVENTS (apps.alerts.events) of a period —
the same Alert rows the bell notified about, filtered, counted and paged in
the database. The on-screen table, the detail popup and the PDF/Excel exports
all go through ``resolve_selection`` + ``alerts_queryset`` so they always
cover exactly the same alerts.

Scoping: ``Alert.client`` is the client that owned the vehicle when the event
happened (like TelemetryEvent.client) — a client user sees only their own
client's alerts; staff/superusers see all (apps.core.scoping). RBAC is the
``alert`` module (view), checked by the views.
"""

import dataclasses
import datetime

from django.db.models import Count, F, Q
from django.utils import timezone

from apps.alerts.models import Alert
from apps.core.scoping import scope_queryset
from apps.core.utils import display_timezone
from apps.tracking import trip_analytics
from apps.tracking.trip_analytics import ReportError
from apps.vehicles.models import Vehicle

PAGE_SIZES = (25, 50, 100)
DEFAULT_PAGE_SIZE = 50
PDF_MAX_ROWS = 20_000
EXCEL_MAX_ROWS = 1_000_000

TYPE_CHOICES = [(c.value, c.label) for c in Alert.TELEMETRY_CATEGORIES]
STATUS_CHOICES = list(Alert.Status.choices)
STATUS_LABELS = {Alert.Status.OPEN: "New", Alert.Status.ACKNOWLEDGED: "Acknowledged", Alert.Status.RESOLVED: "Resolved"}
# "Level" in the UI/exports = Alert.severity (one field; the label is just the report's wording).
LEVEL_CHOICES = list(Alert.Severity.choices)

# Whitelisted server-side sorts; ties fall back to time.
SORTS = {
    "time": "occurred_at", "vehicle": "vehicle__registration_number", "type": "category",
    "status": "status", "speed": "speed", "voltage": "voltage", "level": "severity",
}


@dataclasses.dataclass
class Selection:
    user: object
    vehicle: object  # Vehicle or None (= all vehicles the user can see)
    alert_type: str
    status: str
    level: str  # Alert.severity value, or "" for all
    geofence: object  # Geofence or None (= any)
    range_key: str
    start_date: datetime.date
    end_date: datetime.date
    period_start: datetime.datetime
    period_end: datetime.datetime
    tz: datetime.tzinfo

    @property
    def vehicle_label(self):
        return self.vehicle.registration_number if self.vehicle else "All vehicles"

    @property
    def type_label(self):
        return dict(TYPE_CHOICES).get(self.alert_type, "All alerts")

    @property
    def level_label(self):
        return dict(LEVEL_CHOICES).get(self.level, "All levels")

    @property
    def status_label(self):
        return STATUS_LABELS.get(self.status, "All statuses")


def report_vehicles(user):
    """The vehicle filter's options: every vehicle the user can see that can
    raise telemetry alerts (has a GPS device) or already has some."""
    return (
        scope_queryset(Vehicle.objects.all(), user)
        .filter(Q(tracking_device__isnull=False) | Q(alerts__category__in=Alert.TELEMETRY_CATEGORIES))
        .distinct().order_by("registration_number")
    )


def resolve_selection(*, user, vehicle="", alert_type="", status="", level="", geofence="", range_key="today",
                      from_str="", to_str="", now=None):
    """Strict, like the other report downloads: bad input is a readable error,
    never a silently different selection."""
    now = now or timezone.now()
    tz = display_timezone()
    range_key, start_date, end_date = trip_analytics.validate_export_range(range_key, from_str, to_str, now=now, tz=tz)
    vehicle = (vehicle or "").strip()
    vehicle_obj = None
    if vehicle and vehicle.lower() != "all":
        vehicle_obj = scope_queryset(Vehicle.objects.all(), user).filter(uuid=vehicle).first() if _is_uuid(vehicle) else None
        if vehicle_obj is None:
            raise ReportError("The selected vehicle was not found or you don't have access to it.", status=404)
    alert_type = (alert_type or "").strip().upper()
    if alert_type and alert_type not in dict(TYPE_CHOICES):
        raise ReportError("Unknown alert type.")
    status = (status or "").strip().upper()
    if status and status not in Alert.Status.values:
        raise ReportError("Unknown alert status.")
    level = (level or "").strip().upper()
    if level and level not in Alert.Severity.values:
        raise ReportError("Unknown alert level.")
    geofence = (geofence or "").strip()
    geofence_obj = None
    if geofence and geofence.lower() != "all":
        geofence_obj = report_geofences(user).filter(uuid=geofence).first() if _is_uuid(geofence) else None
        if geofence_obj is None:
            raise ReportError("The selected geofence was not found or you don't have access to it.", status=404)
    period_start = datetime.datetime.combine(start_date, datetime.time.min, tzinfo=tz)
    period_end = datetime.datetime.combine(end_date + datetime.timedelta(days=1), datetime.time.min, tzinfo=tz)
    return Selection(user, vehicle_obj, alert_type, status, level, geofence_obj, range_key, start_date, end_date,
                     period_start, period_end, tz)


def _is_uuid(value):
    import uuid

    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _geofence_ct():
    from django.contrib.contenttypes.models import ContentType

    from apps.geofences.models import Geofence

    return ContentType.objects.get_for_model(Geofence)


def report_geofences(user):
    """The Geofence filter's options: geofences that have alerts the user can
    see — never another client's zone names."""
    from apps.geofences.models import Geofence

    ids = scoped_alerts(user).filter(content_type=_geofence_ct()).values("object_id").distinct()
    return Geofence.objects.filter(pk__in=ids).order_by("name")


def alert_geofence(alert):
    from apps.geofences.models import Geofence

    obj = alert.content_object if alert.content_type_id else None
    return obj if isinstance(obj, Geofence) else None


def scoped_alerts(user):
    """Every telemetry alert ``user`` may see (archived ones included — the
    report is history; archiving only tidies the Alerts work list)."""
    qs = Alert.objects.filter(category__in=Alert.TELEMETRY_CATEGORIES)
    return scope_queryset(qs, user, "client_id")


def alerts_queryset(selection, *, with_status=True):
    qs = scoped_alerts(selection.user).filter(
        occurred_at__gte=selection.period_start, occurred_at__lt=selection.period_end
    )
    if selection.vehicle is not None:
        qs = qs.filter(vehicle=selection.vehicle)
    if selection.alert_type:
        qs = qs.filter(category=selection.alert_type)
    if selection.level:
        qs = qs.filter(severity=selection.level)
    if selection.geofence is not None:
        qs = qs.filter(content_type=_geofence_ct(), object_id=selection.geofence.pk)
    if with_status and selection.status:
        qs = qs.filter(status=selection.status)
    # content_object = the geofence of geofence alerts (one extra query per page, never per row).
    return qs.select_related("vehicle", "driver", "client", "acknowledged_by", "resolved_by").prefetch_related(
        "content_object")


def summary(selection):
    """Counts for the status pills / KPI cards — over the whole selection
    except the status filter itself (so every pill shows its own count)."""
    totals = alerts_queryset(selection, with_status=False).aggregate(
        total=Count("id"),
        open=Count("id", filter=Q(status=Alert.Status.OPEN)),
        acknowledged=Count("id", filter=Q(status=Alert.Status.ACKNOWLEDGED)),
        resolved=Count("id", filter=Q(status=Alert.Status.RESOLVED)),
        signal_active=Count("id", filter=Q(signal_cleared_at__isnull=True)),
        vehicles=Count("vehicle", distinct=True),
    )
    by_type = dict(
        alerts_queryset(selection, with_status=False).order_by().values_list("category").annotate(n=Count("id"))
    )
    totals["by_type"] = {key: by_type.get(key, 0) for key, _label in TYPE_CHOICES}
    by_level = dict(
        alerts_queryset(selection, with_status=False).order_by().values_list("severity").annotate(n=Count("id"))
    )
    totals["by_level"] = {key: by_level.get(key, 0) for key, _label in LEVEL_CHOICES}
    totals["matching"] = totals["total"] if not selection.status else {
        Alert.Status.OPEN: totals["open"], Alert.Status.ACKNOWLEDGED: totals["acknowledged"],
        Alert.Status.RESOLVED: totals["resolved"],
    }[selection.status]
    return totals


def _ordering(sort, direction):
    desc = direction == "desc"
    field = F(SORTS.get(sort, "occurred_at"))
    primary = field.desc(nulls_last=True) if desc else field.asc(nulls_last=True)
    return [primary, "-occurred_at" if desc else "occurred_at", "-id" if desc else "id"]


def page(selection, *, page_number=1, page_size=DEFAULT_PAGE_SIZE, sort="time", direction="desc"):
    page_size = page_size if page_size in PAGE_SIZES else DEFAULT_PAGE_SIZE
    qs = alerts_queryset(selection)
    total = qs.count()
    pages = max(1, -(-total // page_size))
    page_number = min(max(1, page_number), pages)
    offset = (page_number - 1) * page_size
    rows = list(qs.order_by(*_ordering(sort, direction))[offset:offset + page_size])
    return {"total": total, "page": page_number, "pages": pages, "page_size": page_size, "rows": rows}


def iter_alerts(selection):
    """Every matching alert, oldest first, streamed (exports)."""
    return alerts_queryset(selection).order_by("occurred_at", "id").iterator(chunk_size=2000)


def _iso(value):
    return value.isoformat() if value else None


def _num(value):
    return float(value) if value is not None else None


def duration_seconds(alert):
    """How long the condition lasted: start -> ended (panic released / vehicle
    moved), or, while still active, start -> its latest reading. For idle this
    is the real idle time, not the alert threshold."""
    if not alert.occurred_at:
        return None
    end = alert.signal_cleared_at or alert.last_signal_at
    return max(0, int((end - alert.occurred_at).total_seconds())) if end else None


def duration_text(seconds):
    """"12m 30s" / "1h 05m" / "45s" — idle and panic durations are often
    minutes-and-seconds long, so seconds are kept below an hour."""
    if seconds is None:
        return ""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def serialize(alert):
    vehicle = alert.vehicle
    return {
        "uuid": str(alert.uuid),
        "type": alert.category,
        "type_label": alert.get_category_display(),
        "level": alert.severity,
        "level_label": alert.get_severity_display(),
        "status": alert.status,
        "status_label": STATUS_LABELS.get(alert.status, alert.get_status_display()),
        "occurred_at": _iso(alert.occurred_at),
        "triggered_at": _iso(alert.triggered_at or alert.occurred_at),
        "last_signal_at": _iso(alert.last_signal_at),
        "signal_cleared_at": _iso(alert.signal_cleared_at),
        "signal_active": alert.signal_cleared_at is None,
        "duration_seconds": duration_seconds(alert),
        "duration_text": duration_text(duration_seconds(alert)),
        "vehicle_uuid": str(vehicle.uuid) if vehicle else None,
        "registration_number": vehicle.registration_number if vehicle else "",
        "driver": alert.driver.get_full_name() if alert.driver_id else "",
        "client": alert.client.client_name if alert.client_id else "",
        "latitude": _num(alert.latitude),
        "longitude": _num(alert.longitude),
        "location": alert.location,
        "message": alert.message,
        "speed": _num(alert.speed),
        "ignition": alert.ignition,
        "voltage": _num(alert.voltage),
        "speed_limit": alert.speed_limit,
        "geofence": ({"uuid": str(g.uuid), "name": g.name} if (g := alert_geofence(alert)) else None),
        "odometer": _num(alert.odometer),
        "created_at": _iso(alert.created_at),
        "acknowledged_by": alert.acknowledged_by.get_full_name() if alert.acknowledged_by_id else "",
        "acknowledged_at": _iso(alert.acknowledged_at),
        "resolved_by": alert.resolved_by.get_full_name() if alert.resolved_by_id else "",
        "resolved_at": _iso(alert.resolved_at),
    }
