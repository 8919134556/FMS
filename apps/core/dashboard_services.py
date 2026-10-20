"""Query layer for the Operations Command Center dashboard.

Two services, deliberately split by what kind of data they hold:

- ``OperationsService`` — REAL-TIME current state (vehicle/driver status,
  overdue maintenance, expiring documents). Never affected by the
  dashboard's date-range selector — "how many vehicles are on trip right
  now" doesn't have a "yesterday" version.
- ``DashboardService`` — DATE-SCOPED operational activity (today's trips,
  the live trip board, site activity). Driven by the date-range selector.

Kept out of views.py per the standing instruction to avoid dumping all
dashboard calculations into the view — this is also where future
real-time (WebSocket/polling) push would hook in without touching the
view or template layer.

Every method that exposes another module's data checks that module's
"view" permission first and returns ``None`` (not zeros/empty lists) when
the caller can't see it, so DashboardView can tell "no data" apart from
"not permitted to know" and never leaks a restricted count into the page.
"""

import datetime

from django.db.models import Count, F, Prefetch, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.clients.models import Client
from apps.core.models import SystemSettings
from apps.core.permissions import user_has_permission
from apps.core.scoping import client_scope_id, scope_queryset
from apps.documents.models import Document
from apps.documents.services import scope_documents
from apps.drivers.models import Driver
from apps.locations.models import Site
from apps.maintenance.models import Maintenance
from apps.trips.models import Trip
from apps.vehicles.models import Vehicle

DATE_RANGE_CHOICES = {"today", "yesterday", "week", "month"}
LIVE_BOARD_LIMIT = 10
TABLE_LIMIT = 8


def _pluralize(count, noun):
    return f"{count} {noun}{'s' if count != 1 else ''}"


class _ClientScoped:
    """Base querysets for every dashboard widget, already restricted to the
    viewer's client (apps.core.scoping). Every widget must start from one of
    these instead of ``Model.objects`` so a client user's dashboard can never
    count or list another client's data."""

    user = None

    def dispatch_url(self, quick_filter, trip_status):
        """The Dispatch Board is an internal ops tool (staff only); client
        users land on the equivalent Trips list instead of a 403."""
        from django.urls import reverse

        if client_scope_id(self.user) is not None:
            return reverse("trips:trip_list") + f"?status={trip_status}"
        return reverse("dispatch:board") + f"?quick_filter={quick_filter}"

    def vehicles(self):
        return scope_queryset(Vehicle.objects.all(), self.user)

    def drivers(self):
        return scope_queryset(Driver.objects.all(), self.user)

    def trips(self):
        return scope_queryset(Trip.objects.all(), self.user)

    def maintenance(self):
        return scope_queryset(Maintenance.objects.all(), self.user, "vehicle__client_id")

    def documents(self):
        return scope_documents(Document.objects.all(), self.user)

    def clients(self):
        return scope_queryset(Client.objects.all(), self.user, "id")

    def sites(self):
        return scope_queryset(Site.objects.all(), self.user)


class OperationsService(_ClientScoped):
    """Real-time (not date-scoped) fleet operational state."""

    def __init__(self, user):
        self.user = user
        self._permission_cache = {}

    def can_view(self, module):
        """Memoized per instance — several widgets ask about the same
        module (e.g. both the view and the individual card build check
        "vehicle"), and permission checks are their own DB query, so this
        avoids paying for that repeatedly on one dashboard render."""
        if module not in self._permission_cache:
            self._permission_cache[module] = user_has_permission(self.user, module, "view")
        return self._permission_cache[module]

    def fleet_status(self):
        """Total/Available/On Trip/Maintenance/Inactive vehicle counts —
        Section 1 (Fleet Status KPIs) and Section 5 (Vehicle Availability)
        share this single query."""
        if not self.can_view("vehicle"):
            return None
        availability_counts = dict(
            self.vehicles().values_list("availability_status").annotate(count=Count("id"))
        )
        status_counts = dict(self.vehicles().values_list("status").annotate(count=Count("id")))
        total = self.vehicles().count()
        # "Available" must mean genuinely usable right now — status=ACTIVE
        # *and* availability_status=AVAILABLE — not just the raw
        # availability_status flag, which can go stale on a vehicle that's
        # been deactivated/retired/sold without also being reassigned.
        available_count = self.vehicles().filter(
            status=Vehicle.Status.ACTIVE, availability_status=Vehicle.AvailabilityStatus.AVAILABLE
        ).count()
        return {
            "total": total,
            "available": available_count,
            "on_trip": availability_counts.get(Vehicle.AvailabilityStatus.ON_TRIP, 0),
            "maintenance": availability_counts.get(Vehicle.AvailabilityStatus.MAINTENANCE, 0),
            "assigned": availability_counts.get(Vehicle.AvailabilityStatus.ASSIGNED, 0),
            "inactive": status_counts.get(Vehicle.Status.INACTIVE, 0),
            "status_counts": status_counts,
        }

    def driver_availability(self):
        """Total/Available/Assigned/On Trip/Inactive driver counts, plus a
        compact table of the "busy" ones (on trip or assigned) — matches
        how the spec's example rows read ("Assigned to TRP-000123")."""
        if not self.can_view("driver"):
            return None

        active_drivers = self.drivers().filter(employment_status=Driver.EmploymentStatus.ACTIVE)
        total = self.drivers().count()
        inactive = total - active_drivers.count()

        on_trip_driver_ids = set(
            self.trips().filter(
                driver__in=active_drivers, status__in=[Trip.Status.DISPATCHED, Trip.Status.IN_PROGRESS]
            ).values_list("driver_id", flat=True)
        )
        assigned_driver_ids = set(
            active_drivers.filter(current_vehicles__isnull=False)
            .exclude(id__in=on_trip_driver_ids)
            .values_list("id", flat=True)
        )
        available_count = active_drivers.count() - len(on_trip_driver_ids) - len(assigned_driver_ids)

        busy_ids = on_trip_driver_ids | assigned_driver_ids
        table_rows = []
        if busy_ids:
            busy_drivers = (
                active_drivers.filter(id__in=busy_ids)
                .prefetch_related(Prefetch("current_vehicles", to_attr="current_vehicle_list"))
                .order_by("first_name", "last_name")[:TABLE_LIMIT]
            )
            current_trips = {
                t.driver_id: t
                for t in self.trips().filter(
                    driver_id__in=busy_ids, status__in=[Trip.Status.DISPATCHED, Trip.Status.IN_PROGRESS]
                ).select_related("vehicle")
            }
            for driver in busy_drivers:
                trip = current_trips.get(driver.id)
                vehicle = driver.current_vehicle_list[0] if driver.current_vehicle_list else None
                table_rows.append(
                    {
                        "driver": driver,
                        "vehicle": vehicle or (trip.vehicle if trip else None),
                        "trip": trip,
                        "status": "On Trip" if trip else "Assigned",
                    }
                )

        return {
            "total": total,
            "available": max(available_count, 0),
            "assigned": len(assigned_driver_ids),
            "on_trip": len(on_trip_driver_ids),
            "inactive": inactive,
            "table_rows": table_rows,
        }

    def maintenance_overview(self):
        """Reuses Maintenance.objects.overdue()/due_today()/due_soon() —
        the same queryset methods the Maintenance list/detail pages use,
        so this never re-derives the overdue definition."""
        if not self.can_view("maintenance"):
            return None
        due_soon_days = SystemSettings.load().maintenance_due_soon_days
        today = timezone.now().date()
        overdue_count = self.maintenance().overdue().count()
        due_today_count = self.maintenance().due_today().count()
        due_soon_count = self.maintenance().due_soon(days=due_soon_days).count()
        in_progress_count = self.maintenance().filter(status=Maintenance.Status.IN_PROGRESS).count()

        table_rows = list(
            self.maintenance().filter(
                status=Maintenance.Status.SCHEDULED,
                scheduled_date__lte=today + datetime.timedelta(days=due_soon_days),
            )
            .select_related("vehicle")
            .order_by("scheduled_date")[:TABLE_LIMIT]
        )
        return {
            "overdue": overdue_count,
            "due_today": due_today_count,
            "due_soon": due_soon_count,
            "in_progress": in_progress_count,
            "table_rows": table_rows,
        }

    def document_compliance(self):
        """Reuses Document.objects.expired()/expiring_soon() — same
        queryset methods the Documents list/dashboard-widget already use."""
        if not self.can_view("document"):
            return None
        expiry_warning_days = SystemSettings.load().document_expiry_warning_days
        today = timezone.now().date()
        expired_qs = self.documents().expired().select_related("content_type")
        expiring_7_qs = self.documents().filter(
            status=Document.Status.ACTIVE, expiry_date__gte=today, expiry_date__lte=today + datetime.timedelta(days=7)
        )
        expiring_30_qs = self.documents().expiring_soon(days=expiry_warning_days)

        table_rows = list(
            self.documents().filter(status=Document.Status.ACTIVE, expiry_date__isnull=False)
            .filter(expiry_date__lte=today + datetime.timedelta(days=expiry_warning_days))
            .select_related("content_type")
            .prefetch_related("content_object")
            .order_by("expiry_date")[:TABLE_LIMIT]
        )
        return {
            "expired": expired_qs.count(),
            "expiring_7_days": expiring_7_qs.count(),
            "expiring_30_days": expiring_30_qs.count(),
            "table_rows": table_rows,
        }

    def client_operations(self):
        if not self.can_view("client"):
            return None
        active_count = self.clients().filter(status=Client.Status.ACTIVE).count()
        # Deliberately excludes DELAYED — that's its own KPI bucket below,
        # not a sub-case of "active". A client shouldn't be counted in both.
        with_active_trips = self.clients().filter(
            trips__status__in=[Trip.Status.ASSIGNED, Trip.Status.DISPATCHED, Trip.Status.IN_PROGRESS]
        ).distinct().count()
        with_delayed_trips = self.clients().filter(trips__status=Trip.Status.DELAYED).distinct().count()
        with_maintenance_issues = 0
        if self.can_view("maintenance"):
            with_maintenance_issues = self.clients().filter(
                vehicles__maintenance_records__in=self.maintenance().overdue()
            ).distinct().count()
        return {
            "active_clients": active_count,
            "with_active_trips": with_active_trips,
            "with_delayed_trips": with_delayed_trips,
            "with_maintenance_issues": with_maintenance_issues,
        }

    def attention_items(self, account_hygiene_items=None):
        """Operational alerts — delayed trips, overdue maintenance,
        expiring/expired documents, vehicles under maintenance, unassigned
        scheduled trips — appended to the existing account-hygiene alerts
        (locked/suspended users etc.) so there's one unified panel instead
        of two competing ones. Each item is only computed if the viewer
        can see that module."""
        from django.urls import reverse

        items = list(account_hygiene_items or [])

        if self.can_view("trip"):
            delayed_count = self.trips().filter(status=Trip.Status.DELAYED).count()
            if delayed_count:
                items.append(
                    {
                        "severity": "high",
                        "icon": "exclamation-triangle",
                        "title": f"{_pluralize(delayed_count, 'Delayed Trip')}",
                        "description": "Trips currently behind schedule.",
                        "url": self.dispatch_url("delayed", "DELAYED"),
                    }
                )
            unassigned_count = self.trips().filter(status=Trip.Status.SCHEDULED).count()
            if unassigned_count:
                items.append(
                    {
                        "severity": "medium",
                        "icon": "person-x",
                        "title": f"{_pluralize(unassigned_count, 'Unassigned Trip')}",
                        "description": "Scheduled trips still need a vehicle and driver.",
                        "url": self.dispatch_url("needs_assignment", "SCHEDULED"),
                    }
                )

        if self.can_view("maintenance"):
            overdue_count = self.maintenance().overdue().count()
            if overdue_count:
                items.append(
                    {
                        "severity": "critical",
                        "icon": "tools",
                        "title": f"{_pluralize(overdue_count, 'Overdue Maintenance')}",
                        "description": "Vehicles require service.",
                        "url": reverse("maintenance:maintenance_list") + "?status=OVERDUE",
                    }
                )

        if self.can_view("vehicle"):
            under_maintenance_count = self.vehicles().filter(status=Vehicle.Status.UNDER_MAINTENANCE).count()
            if under_maintenance_count:
                items.append(
                    {
                        "severity": "medium",
                        "icon": "wrench-adjustable",
                        "title": f"{_pluralize(under_maintenance_count, 'Vehicle')} Under Maintenance",
                        "description": "Currently out of service for repair.",
                        "url": reverse("vehicles:vehicle_list") + "?status=UNDER_MAINTENANCE",
                    }
                )

        if self.can_view("document"):
            expired_count = self.documents().expired().count()
            if expired_count:
                items.append(
                    {
                        "severity": "critical",
                        "icon": "file-earmark-excel",
                        "title": f"{_pluralize(expired_count, 'Expired Document')}",
                        "description": "Documents have passed their expiry date.",
                        "url": reverse("documents:document_list") + "?expiry=expired",
                    }
                )
            expiring_count = self.documents().expiring_soon().count()
            if expiring_count:
                items.append(
                    {
                        "severity": "low",
                        "icon": "file-earmark-text",
                        "title": f"{_pluralize(expiring_count, 'Document')} Expiring Soon",
                        "description": "Documents require renewal.",
                        "url": reverse("documents:document_list") + "?expiry=expiring_soon",
                    }
                )

        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        items.sort(key=lambda item: severity_order[item["severity"]])
        return items


class DashboardService(_ClientScoped):
    """Date-scoped operational activity — driven by the dashboard's date
    range selector (today/yesterday/week/month). Recent Trips is the one
    exception: "recent" is inherently its own window, not the selected one."""

    def __init__(self, user, date_range="today"):
        self.user = user
        self.date_range = date_range if date_range in DATE_RANGE_CHOICES else "today"
        self.start, self.end = self._resolve_range()
        self._permission_cache = {}

    def can_view(self, module):
        if module not in self._permission_cache:
            self._permission_cache[module] = user_has_permission(self.user, module, "view")
        return self._permission_cache[module]

    def _resolve_range(self):
        today = timezone.now().date()
        if self.date_range == "yesterday":
            d = today - datetime.timedelta(days=1)
            return d, d
        if self.date_range == "week":
            return today - datetime.timedelta(days=today.weekday()), today
        if self.date_range == "month":
            return today.replace(day=1), today
        return today, today

    def trip_status_summary(self):
        """Today's/selected-range Trip counts by status, for the clickable
        "Today's Operations" mini-cards."""
        if not self.can_view("trip"):
            return None
        counts = dict(
            self.trips().filter(scheduled_start__date__gte=self.start, scheduled_start__date__lte=self.end)
            .values_list("status")
            .annotate(count=Count("id"))
        )
        return {
            "scheduled": counts.get(Trip.Status.SCHEDULED, 0),
            "assigned": counts.get(Trip.Status.ASSIGNED, 0),
            "dispatched": counts.get(Trip.Status.DISPATCHED, 0),
            "in_progress": counts.get(Trip.Status.IN_PROGRESS, 0),
            "completed": counts.get(Trip.Status.COMPLETED, 0),
            "delayed": counts.get(Trip.Status.DELAYED, 0),
            "cancelled": counts.get(Trip.Status.CANCELLED, 0),
            "total": sum(counts.values()),
        }

    def active_trips_board(self):
        """The Live Operational Board — trips scheduled in the selected
        range, most operationally relevant (active) statuses first."""
        if not self.can_view("trip"):
            return None
        return list(
            self.trips().filter(scheduled_start__date__gte=self.start, scheduled_start__date__lte=self.end)
            .select_related("client", "vehicle", "driver", "origin_site", "destination_site")
            .order_by("scheduled_start")[:LIVE_BOARD_LIMIT]
        )

    def recent_trips(self):
        """Fixes the dashboard's previous stale "Recent Trips arrives in
        Phase 6" placeholder — real, always-recent (not date-range-scoped)
        activity regardless of what the operations date filter is set to."""
        if not self.can_view("trip"):
            return None
        return list(
            self.trips().select_related("client", "vehicle", "driver", "origin_site", "destination_site")
            .order_by("-created_at")[:8]
        )

    def site_operations(self):
        """Most active sites in the selected range — pure DB aggregation,
        no Python-side looping over sites to count trips."""
        if not self.can_view("site") or not self.can_view("trip"):
            return None
        sites = (
            self.sites().annotate(
                outbound=Count(
                    "trips_as_origin",
                    filter=Q(trips_as_origin__scheduled_start__date__gte=self.start, trips_as_origin__scheduled_start__date__lte=self.end),
                    distinct=True,
                ),
                inbound=Count(
                    "trips_as_destination",
                    filter=Q(trips_as_destination__scheduled_start__date__gte=self.start, trips_as_destination__scheduled_start__date__lte=self.end),
                    distinct=True,
                ),
            )
            .annotate(trips_total=F("outbound") + F("inbound"))
            .filter(trips_total__gt=0)
            .select_related("client")
            .order_by("-trips_total")[:TABLE_LIMIT]
        )
        return list(sites)

    def trip_trend(self, days=7):
        """Trips scheduled per day (and how many of those completed) over
        the last ``days`` days ending today — one grouped query, not one
        per day. Independent of the date-range selector: it's a trend line,
        not a snapshot."""
        if not self.can_view("trip"):
            return None
        today = timezone.now().date()
        start = today - datetime.timedelta(days=days - 1)
        rows = (
            self.trips().filter(scheduled_start__date__gte=start, scheduled_start__date__lte=today)
            .annotate(day=TruncDate("scheduled_start"))
            .values("day")
            .annotate(total=Count("id"), completed=Count("id", filter=Q(status=Trip.Status.COMPLETED)))
        )
        by_day = {row["day"]: row for row in rows}
        labels, total, completed = [], [], []
        for offset in range(days):
            day = start + datetime.timedelta(days=offset)
            labels.append(day.strftime("%b %d"))
            total.append(by_day.get(day, {}).get("total", 0))
            completed.append(by_day.get(day, {}).get("completed", 0))
        return {"labels": labels, "total": total, "completed": completed}


# ---------------------------------------------------------------------------
# Live fleet + alerts (telemetry-driven). Each function checks its own module
# permission and returns None when the caller can't see it; data comes from the
# same services the Live Tracking page, Trip Report and Alert Report use, so
# the dashboard can never disagree with them.
# ---------------------------------------------------------------------------

LIVE_STATUS_KEYS = ("moving", "idle", "stopped", "offline")
LIVE_STATUS_LABELS = {"moving": "Moving", "idle": "Idle", "stopped": "Ignition Off", "offline": "Offline"}
ALERT_LEVELS = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
RECENT_ALERTS_LIMIT = 6
RECENT_TRIPS_LIMIT = 5
ACTIVITY_FEED_LIMIT = 8


def live_status_key(vehicle):
    """Same rule as the Live Tracking marker (static/js/vehicle_icons.js statusFor)."""
    if vehicle.get("connection_status") == "OFFLINE":
        return "offline"
    if vehicle.get("movement_state") == "MOVING":
        return "moving"
    if vehicle.get("ignition") is False:
        return "stopped"
    return "idle"


def live_fleet_summary(user):
    """Counts by live status from the fleet feed (apps.tracking.services.fleet_current_telemetry)."""
    if not user_has_permission(user, "tracking_device", "view"):
        return None
    from apps.tracking.services import fleet_current_telemetry

    rows = fleet_current_telemetry(user)
    counts = dict.fromkeys(LIVE_STATUS_KEYS, 0)
    for vehicle in rows:
        counts[live_status_key(vehicle)] += 1
    tracked = len(rows)
    return {
        "tracked": tracked,
        **counts,
        "online": sum(1 for v in rows if v.get("connection_status") == "ONLINE"),
        "immobilized": sum(1 for v in rows if v.get("immobilizer") is True),
        "alerting": sum(1 for v in rows if v.get("active_alert")),
        "share": {key: round(counts[key] * 100 / tracked) if tracked else 0 for key in LIVE_STATUS_KEYS},
    }


def todays_trip_activity(user):
    """Today's ignition-based trips + distance — the Trip Report's own computation — plus the
    performance figures that follow from it: utilisation (vehicles with a trip / GPS-equipped
    vehicles) and the average moving speed of today's readings."""
    if not user_has_permission(user, "tracking_device", "view"):
        return None
    from django.conf import settings
    from django.db.models import Avg

    from apps.core.utils import display_timezone
    from apps.tracking.models import TelemetryEvent
    from apps.tracking.trip_report import compute_vehicle_trips, scoped_tracked_vehicles

    tz = display_timezone()
    now = timezone.now()
    today = now.astimezone(tz).date()
    trips, summary = compute_vehicle_trips(user=user, start_date=today, end_date=today, now=now)
    equipped = scoped_tracked_vehicles(user)
    with_trips = len({t.vehicle.id for t in trips})
    day_start = datetime.datetime.combine(today, datetime.time.min, tzinfo=tz)
    avg_speed = (
        TelemetryEvent.objects.filter(
            vehicle_id__in=[v.id for v in equipped], timestamp__gte=day_start,
            speed__gt=settings.TELEMATICS_MOVEMENT_SPEED_THRESHOLD_KMH,
        ).aggregate(v=Avg("speed"))["v"]
        if equipped else None
    )
    return {
        "total_trips": summary["total_trips"],
        "active_trips": summary["active_trips"],
        "distance_km": summary["total_distance_km"],
        "avg_duration_seconds": summary["avg_duration_seconds"],
        "avg_duration_text": _duration_text(summary["avg_duration_seconds"]),
        "vehicles_with_trips": with_trips,
        "equipped_vehicles": len(equipped),
        "utilization_pct": round(with_trips * 100 / len(equipped)) if equipped else None,
        "avg_moving_speed_kmh": round(float(avg_speed)) if avg_speed is not None else None,
        "recent_trips": [
            {
                "vehicle": t.vehicle.registration_number,
                "vehicle_uuid": str(t.vehicle.uuid),
                "status": t.status,
                "start_at": t.start_at,
                "end_at": t.end_at,
                "distance_km": t.distance_km,
                "start_location": t.start_location,
            }
            for t in trips[:RECENT_TRIPS_LIMIT]  # newest first
        ],
    }


def _duration_text(seconds):
    if not seconds:
        return "—"
    minutes = int(seconds) // 60
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def alerts_overview(user, days=7):
    """Open alerts by level, conditions still active, a per-day trend by level and the latest
    alerts — scoped exactly like the Alert Report (apps.alerts.report.scoped_alerts)."""
    if not user_has_permission(user, "alert", "view"):
        return None
    from apps.alerts.models import Alert
    from apps.alerts.report import scoped_alerts
    from apps.core.utils import display_timezone

    tz = display_timezone()
    alerts = scoped_alerts(user)
    open_by_level = dict(
        alerts.filter(status=Alert.Status.OPEN, is_archived=False).order_by().values_list("severity").annotate(n=Count("id"))
    )
    active_now = alerts.filter(
        category__in=Alert.LIVE_ATTENTION_CATEGORIES, signal_cleared_at__isnull=True, is_archived=False
    ).exclude(status=Alert.Status.RESOLVED).count()

    today = timezone.now().astimezone(tz).date()
    first_day = today - datetime.timedelta(days=days - 1)
    start = datetime.datetime.combine(first_day, datetime.time.min, tzinfo=tz)
    per_day = {}
    for occurred_at, severity in alerts.filter(occurred_at__gte=start).values_list("occurred_at", "severity"):
        key = (occurred_at.astimezone(tz).date(), severity)
        per_day[key] = per_day.get(key, 0) + 1
    day_list = [first_day + datetime.timedelta(days=i) for i in range(days)]
    trend = {
        "labels": [d.strftime("%d %b") for d in day_list],
        "series": {level: [per_day.get((d, level), 0) for d in day_list] for level in ALERT_LEVELS},
    }
    labels = dict(Alert.Category.choices)
    recent = [
        {
            "uuid": str(a.uuid),
            "type": labels.get(a.category, a.category),
            "category": a.category,
            "level": a.severity,
            "level_label": a.get_severity_display(),
            "vehicle": a.vehicle.registration_number if a.vehicle_id else "",
            "occurred_at": a.triggered_at or a.occurred_at,
            "active": a.signal_cleared_at is None,
            "status": a.get_status_display(),
        }
        for a in alerts.select_related("vehicle").order_by("-occurred_at")[:RECENT_ALERTS_LIMIT]
    ]
    return {
        "open_total": sum(open_by_level.values()),
        "open_by_level": [(level, open_by_level.get(level, 0)) for level in ALERT_LEVELS],
        "active_now": active_now,
        "week_total": sum(per_day.values()),
        "trend": trend,
        "recent": recent,
    }


def fleet_activity_24h(user):
    """Per hour, over the last 24 h, from the stored GPS history: how many vehicles reported and how
    many were moving (speed above the movement threshold), plus vehicle alerts raised that hour.
    Two aggregate queries; None without tracking_device.view."""
    if not user_has_permission(user, "tracking_device", "view"):
        return None
    from django.conf import settings
    from django.db.models.functions import TruncHour

    from apps.core.utils import display_timezone
    from apps.tracking.models import TelemetryEvent

    tz = display_timezone()
    now_local = timezone.now().astimezone(tz)
    first_hour = now_local.replace(minute=0, second=0, microsecond=0) - datetime.timedelta(hours=23)
    hours = [first_hour + datetime.timedelta(hours=i) for i in range(24)]
    vehicle_ids = scope_queryset(Vehicle.objects.all(), user).values("id")
    rows = (
        TelemetryEvent.objects.filter(vehicle_id__in=vehicle_ids, timestamp__gte=first_hour)
        .annotate(h=TruncHour("timestamp", tzinfo=tz)).values("h")
        .annotate(
            reporting=Count("vehicle", distinct=True),
            moving=Count("vehicle", distinct=True, filter=Q(speed__gt=settings.TELEMATICS_MOVEMENT_SPEED_THRESHOLD_KMH)),
        )
    )
    by_hour = {r["h"].astimezone(tz).replace(minute=0, second=0, microsecond=0): r for r in rows}
    alerts_by_hour = {}
    if user_has_permission(user, "alert", "view"):
        from apps.alerts.report import scoped_alerts

        for r in (scoped_alerts(user).filter(occurred_at__gte=first_hour)
                  .annotate(h=TruncHour("occurred_at", tzinfo=tz)).values("h").annotate(n=Count("id"))):
            alerts_by_hour[r["h"].astimezone(tz).replace(minute=0, second=0, microsecond=0)] = r["n"]
    reporting = [by_hour.get(h, {}).get("reporting", 0) for h in hours]
    return {
        "labels": [h.strftime("%H:00") for h in hours],
        "reporting": reporting,
        "moving": [by_hour.get(h, {}).get("moving", 0) for h in hours],
        "alerts": [alerts_by_hour.get(h, 0) for h in hours] if user_has_permission(user, "alert", "view") else None,
        "has_data": any(reporting) or any(alerts_by_hour.values()),
    }


def recent_activity_feed(alerts_info, trip_activity):
    """Latest alerts + today's trips, newest first, for the dashboard timeline (no extra queries)."""
    items = []
    for a in (alerts_info or {}).get("recent", []):
        if a["occurred_at"] is None:
            continue
        items.append({
            "kind": "alert", "at": a["occurred_at"], "title": a["type"], "vehicle": a["vehicle"],
            "level": a["level"], "level_label": a["level_label"], "active": a["active"], "status": a["status"],
            "url_alert": a["uuid"],
        })
    for t in (trip_activity or {}).get("recent_trips", []):
        ended = t["status"] != "ACTIVE" and t["end_at"]
        items.append({
            "kind": "trip", "at": t["end_at"] if ended else t["start_at"],
            "title": "Trip completed" if ended else "Trip started", "vehicle": t["vehicle"],
            "distance_km": t["distance_km"], "active": t["status"] == "ACTIVE", "place": t["start_location"],
        })
    items.sort(key=lambda i: i["at"], reverse=True)
    return items[:ACTIVITY_FEED_LIMIT]


GPS_TRIPS_TABLE_LIMIT = 8


def gps_trips_week(user, days=7):
    """GPS-detected trips (the Trip Report's own detection) for the dashboard's trip widgets: trips and
    distance per day over the last ``days`` days, the trips in progress now and the latest trips.
    The Trips module (planned/dispatched trips) is separate; these are what the vehicles actually drove."""
    if not user_has_permission(user, "tracking_device", "view"):
        return None
    from apps.core.utils import display_timezone
    from apps.tracking.trip_report import compute_vehicle_trips, resolve_trip_drivers

    tz = display_timezone()
    now = timezone.now()
    today = now.astimezone(tz).date()
    first_day = today - datetime.timedelta(days=days - 1)
    trips, summary = compute_vehicle_trips(user=user, start_date=first_day, end_date=today, now=now)
    day_list = [first_day + datetime.timedelta(days=i) for i in range(days)]
    per_day = {d: [0, 0.0] for d in day_list}
    for t in trips:
        day = t.start_at.astimezone(tz).date()
        if day in per_day:
            per_day[day][0] += 1
            per_day[day][1] += float(t.distance_km or 0)
    drivers = resolve_trip_drivers(trips, tz=tz)

    def row(t):
        driver = drivers.get(id(t))
        return {
            "vehicle": t.vehicle.registration_number,
            "driver": driver.get_full_name() if driver else "",
            "status": t.status,
            "start_at": t.start_at,
            "end_at": t.end_at,
            "start_location": t.start_location,
            "end_location": t.end_location,
            "distance_km": t.distance_km,
            "duration_text": _duration_text(t.duration(now=now).total_seconds()),
        }

    return {
        "total": summary["total_trips"],
        "distance_km": summary["total_distance_km"],
        "trend": {
            "labels": [d.strftime("%d %b") for d in day_list],
            "trips": [per_day[d][0] for d in day_list],
            "distance_km": [round(per_day[d][1], 1) for d in day_list],
        },
        "active": [row(t) for t in trips if t.status == "ACTIVE"],
        "recent": [row(t) for t in trips[:GPS_TRIPS_TABLE_LIMIT]],  # newest first
    }
