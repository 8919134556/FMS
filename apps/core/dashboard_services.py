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
