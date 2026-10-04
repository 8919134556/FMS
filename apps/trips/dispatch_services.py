"""Query/aggregation layer for the Dispatch Board (Phase 3.2).

This module is a pure orchestration/read layer over apps.trips.services and
apps.trips.models — it never mutates a Trip and never re-derives the
transition/validation rules that already live in services.py. All state
changes on the Dispatch Board go through the existing apps.trips.views
lifecycle endpoints (trip_assign/trip_dispatch/trip_start/trip_delay/
trip_resume/trip_complete), which call straight into apps.trips.services.

Two kinds of data, same split as apps.core.dashboard_services:
- The board columns/filters are DATE-SCOPED (driven by the board's
  Previous/Today/Next date-nav).
- Dispatch alerts are REAL-TIME (never date-scoped) — a past-due trip from
  three days ago is still a live problem regardless of which day the board
  is currently showing.
"""

import datetime

from django.db.models import Count, OuterRef, Q, Subquery
from django.urls import reverse
from django.utils import timezone

from apps.clients.models import Client
from apps.core.permissions import user_has_permission
from apps.drivers.models import Driver
from apps.maintenance.models import Maintenance
from apps.trips.models import Trip
from apps.trips.views import TRIP_SELECT_RELATED
from apps.vehicles.models import Vehicle, VehicleType

# Single centralized "starting soon" window — every alert/template
# computation imports this symbol instead of hardcoding the number of hours.
STARTING_SOON_WINDOW_HOURS = 2

# The five real workflow columns. DRAFT/COMPLETED/CANCELLED never appear on
# the board — completed/cancelled trips stay in the existing Trip list view.
BOARD_COLUMNS = [
    ("needs_assignment", "Needs Assignment", Trip.Status.SCHEDULED),
    ("ready", "Ready to Dispatch", Trip.Status.ASSIGNED),
    ("dispatched", "Dispatched", Trip.Status.DISPATCHED),
    ("in_progress", "In Progress", Trip.Status.IN_PROGRESS),
    ("delayed", "Delayed", Trip.Status.DELAYED),
]
BOARD_STATUSES = [status for _key, _label, status in BOARD_COLUMNS]

QUICK_FILTER_STATUS = {
    "needs_assignment": Trip.Status.SCHEDULED,
    "ready": Trip.Status.ASSIGNED,
    "in_progress": Trip.Status.IN_PROGRESS,
    "delayed": Trip.Status.DELAYED,
}

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def board_queryset(selected_date, get_params, *, apply_quick_filter=True):
    """One queryset for the whole board — date scope + quick-filter +
    regular filters are all just more WHERE clauses on the same query, never
    separate per-column queries. ``apply_quick_filter=False`` gives
    quick_filter_counts() the "every filter except quick_filter" scope it
    needs so pill counts reflect the operator's other active filters
    without being zeroed out by whichever pill is currently selected."""
    qs = Trip.objects.filter(
        status__in=BOARD_STATUSES, scheduled_start__date=selected_date
    ).select_related(*TRIP_SELECT_RELATED)

    if apply_quick_filter:
        quick_filter = get_params.get("quick_filter", "").strip()
        if quick_filter in QUICK_FILTER_STATUS:
            qs = qs.filter(status=QUICK_FILTER_STATUS[quick_filter])
        elif quick_filter == "past_due":
            qs = qs.filter(
                status__in=[Trip.Status.SCHEDULED, Trip.Status.ASSIGNED],
                scheduled_start__lt=timezone.now(),
            )

    query = get_params.get("q", "").strip()
    if query:
        qs = qs.filter(
            Q(trip_number__icontains=query)
            | Q(client__client_name__icontains=query)
            | Q(vehicle__registration_number__icontains=query)
            | Q(driver__first_name__icontains=query)
            | Q(driver__last_name__icontains=query)
            | Q(origin_site__site_name__icontains=query)
            | Q(destination_site__site_name__icontains=query)
        )

    client = get_params.get("client", "").strip()
    if client:
        qs = qs.filter(client_id=client)

    status = get_params.get("status", "").strip()
    if status:
        qs = qs.filter(status=status)

    priority = get_params.get("priority", "").strip()
    if priority:
        qs = qs.filter(priority=priority)

    trip_type = get_params.get("trip_type", "").strip()
    if trip_type:
        qs = qs.filter(trip_type=trip_type)

    vehicle_type = get_params.get("vehicle_type", "").strip()
    if vehicle_type:
        qs = qs.filter(vehicle__vehicle_type_id=vehicle_type)

    driver = get_params.get("driver", "").strip()
    if driver:
        qs = qs.filter(driver_id=driver)

    start_time_from = get_params.get("start_time_from", "").strip()
    if start_time_from:
        qs = qs.filter(scheduled_start__time__gte=start_time_from)

    start_time_to = get_params.get("start_time_to", "").strip()
    if start_time_to:
        qs = qs.filter(scheduled_start__time__lte=start_time_to)

    return qs.order_by("scheduled_start")


def group_into_columns(trips):
    """Evaluates the board queryset exactly once, then buckets it in Python —
    five columns from one query, not five queries."""
    trips = list(trips)
    columns = []
    for key, label, status in BOARD_COLUMNS:
        bucket = [t for t in trips if t.status == status]
        columns.append({"key": key, "label": label, "status": status, "count": len(bucket), "trips": bucket})
    return columns


def quick_filter_counts(selected_date, get_params):
    """Real counts for the quick-filter pill bar, computed from every filter
    EXCEPT quick_filter itself (so picking "Delayed" doesn't zero out what
    "Ready" would show) — two queries total (one grouped-by-status, one for
    the computed "past_due" condition), never one query per pill."""
    base_qs = board_queryset(selected_date, get_params, apply_quick_filter=False)
    # .order_by("scheduled_start") is baked into board_queryset()'s return
    # value — left in place, Django folds it into GROUP BY, fragmenting the
    # count per distinct timestamp instead of summing per status. Clear it
    # before grouping (this doesn't affect column rendering, which uses its
    # own separately-evaluated queryset).
    status_counts = dict(base_qs.order_by().values_list("status").annotate(count=Count("id")))
    past_due_count = base_qs.filter(
        status__in=[Trip.Status.SCHEDULED, Trip.Status.ASSIGNED], scheduled_start__lt=timezone.now()
    ).count()
    return {
        "all": sum(status_counts.values()),
        "needs_assignment": status_counts.get(Trip.Status.SCHEDULED, 0),
        "ready": status_counts.get(Trip.Status.ASSIGNED, 0),
        "in_progress": status_counts.get(Trip.Status.IN_PROGRESS, 0),
        "delayed": status_counts.get(Trip.Status.DELAYED, 0),
        "past_due": past_due_count,
    }


def available_vehicles():
    """Same "genuinely available" predicate as
    apps.core.dashboard_services.OperationsService.fleet_status() — the
    board and the dashboard must never disagree about what "available"
    means. ``next_assignment`` is a single Subquery, not a per-row query."""
    now = timezone.now()
    next_trip_sq = (
        Trip.objects.filter(vehicle=OuterRef("pk"), status__in=Trip.ACTIVE_ASSIGNMENT_STATUSES, scheduled_start__gte=now)
        .order_by("scheduled_start")
        .values("trip_number")[:1]
    )
    return (
        Vehicle.objects.filter(status=Vehicle.Status.ACTIVE, availability_status=Vehicle.AvailabilityStatus.AVAILABLE)
        .select_related("vehicle_type", "client")
        .annotate(next_assignment=Subquery(next_trip_sq))
        .order_by("registration_number")
    )


def available_drivers():
    """Mirrors assign_trip's driver-eligibility predicate: active employment
    and a non-expired license. ``next_assignment`` via the same Subquery
    pattern as available_vehicles()."""
    now = timezone.now()
    today = now.date()
    next_trip_sq = (
        Trip.objects.filter(driver=OuterRef("pk"), status__in=Trip.ACTIVE_ASSIGNMENT_STATUSES, scheduled_start__gte=now)
        .order_by("scheduled_start")
        .values("trip_number")[:1]
    )
    return (
        Driver.objects.filter(employment_status=Driver.EmploymentStatus.ACTIVE)
        .exclude(license_expiry_date__lt=today)
        .select_related("client")
        .annotate(next_assignment=Subquery(next_trip_sq))
        .order_by("first_name", "last_name")
    )


def _trip_url(trip):
    return reverse("trips:trip_detail", kwargs={"uuid": trip.uuid})


def _overlap_alerts(active_trips):
    """Vehicle/driver double-booking that slipped past assign_trip's own
    overlap check — e.g. a plain TripForm edit, which has no overlap
    validation of its own. Scans in Python over an already-fetched, bounded
    (today's active-assignment trips) list — no per-pair query."""
    alerts = []
    for field, label, icon in (("vehicle_id", "Vehicle", "truck-front"), ("driver_id", "Driver", "person-badge")):
        buckets = {}
        for trip in active_trips:
            resource_id = getattr(trip, field)
            if resource_id:
                buckets.setdefault(resource_id, []).append(trip)
        for trips in buckets.values():
            trips.sort(key=lambda t: t.scheduled_start)
            for earlier, later in zip(trips, trips[1:]):
                if earlier.scheduled_end > later.scheduled_start:
                    resource = earlier.vehicle if field == "vehicle_id" else earlier.driver
                    alerts.append(
                        {
                            "severity": "critical",
                            "category": "conflict",
                            "icon": icon,
                            "title": f"{label} conflict: {earlier.trip_number} / {later.trip_number}",
                            "description": f"{resource} is booked on overlapping trips.",
                            "url": _trip_url(earlier),
                        }
                    )
    return alerts


def _maintenance_alerts(active_trips):
    """Vehicles committed to an active trip that are also under maintenance
    or due/overdue for it — a real dispatch-blocking conflict."""
    vehicles_on_trip = {trip.vehicle_id: trip for trip in active_trips if trip.vehicle_id}
    if not vehicles_on_trip:
        return []
    under_maintenance_ids = set(
        Vehicle.objects.filter(id__in=vehicles_on_trip, status=Vehicle.Status.UNDER_MAINTENANCE).values_list(
            "id", flat=True
        )
    )
    due_ids = set(
        Maintenance.objects.filter(vehicle_id__in=vehicles_on_trip)
        .filter(Q(pk__in=Maintenance.objects.overdue()) | Q(pk__in=Maintenance.objects.due_today()))
        .values_list("vehicle_id", flat=True)
    )
    alerts = []
    for vehicle_id in under_maintenance_ids | due_ids:
        trip = vehicles_on_trip[vehicle_id]
        alerts.append(
            {
                "severity": "high",
                "category": "conflict",
                "icon": "tools",
                "title": f"{trip.vehicle.registration_number} has a maintenance conflict",
                "description": f"Committed to trip {trip.trip_number} while under/due for maintenance.",
                "url": _trip_url(trip),
            }
        )
    return alerts


def _license_alerts(active_trips):
    """Drivers already committed to an active trip whose license is expired
    or expiring soon — driver is already select_related, so this is a
    zero-extra-query Python filter."""
    alerts = []
    for trip in active_trips:
        driver = trip.driver
        if driver and driver.license_expiry_status in ("EXPIRED", "EXPIRING_SOON"):
            alerts.append(
                {
                    "severity": "critical" if driver.license_expiry_status == "EXPIRED" else "medium",
                    "category": "conflict",
                    "icon": "person-vcard",
                    "title": f"{driver.get_full_name()}'s license {driver.license_expiry_status.replace('_', ' ').lower()}",
                    "description": f"Assigned to trip {trip.trip_number}.",
                    "url": _trip_url(trip),
                }
            )
    return alerts


def dispatch_alerts(user):
    """Real-time operational problems — never date-scoped (mirrors
    apps.core.dashboard_services.OperationsService, which is deliberately
    unaffected by the dashboard's date-range selector for the same reason).
    """
    if not user_has_permission(user, "trip", "view"):
        return []

    now = timezone.now()
    alerts = []

    starting_soon = Trip.objects.filter(
        status=Trip.Status.SCHEDULED,
        scheduled_start__gte=now,
        scheduled_start__lte=now + datetime.timedelta(hours=STARTING_SOON_WINDOW_HOURS),
    ).select_related("client")
    for trip in starting_soon:
        alerts.append(
            {
                "severity": "high",
                "category": "attention",
                "icon": "clock-history",
                "title": f"{trip.trip_number} starts soon, unassigned",
                "description": f"Scheduled {timezone.localtime(trip.scheduled_start):%H:%M} with no vehicle or driver.",
                "url": _trip_url(trip),
            }
        )

    partial = Trip.objects.filter(
        Q(vehicle__isnull=False, driver__isnull=True) | Q(driver__isnull=False, vehicle__isnull=True),
        status__in=BOARD_STATUSES,
    ).select_related("client")
    for trip in partial:
        missing = "driver" if trip.vehicle_id else "vehicle"
        alerts.append(
            {
                "severity": "medium",
                "category": "attention",
                "icon": "exclamation-diamond",
                "title": f"{trip.trip_number} missing a {missing}",
                "description": f"Trip is only partially assigned — no {missing} on record.",
                "url": _trip_url(trip),
            }
        )

    delayed_count = Trip.objects.filter(status=Trip.Status.DELAYED).count()
    if delayed_count:
        alerts.append(
            {
                "severity": "high",
                "category": "attention",
                "icon": "exclamation-triangle",
                "title": f"{delayed_count} delayed trip{'s' if delayed_count != 1 else ''}",
                "description": "Trips currently behind schedule.",
                "url": reverse("dispatch:board") + "?quick_filter=delayed",
            }
        )

    past_due = Trip.objects.filter(
        status__in=[Trip.Status.SCHEDULED, Trip.Status.ASSIGNED], scheduled_start__lt=now
    ).select_related("client")
    for trip in past_due:
        alerts.append(
            {
                "severity": "critical",
                "category": "attention",
                "icon": "calendar-x",
                "title": f"{trip.trip_number} is past due",
                "description": (
                    f"Scheduled to start {timezone.localtime(trip.scheduled_start):%b %d, %H:%M} "
                    f"and still {trip.get_status_display()}."
                ),
                "url": _trip_url(trip),
            }
        )

    active_trips = list(
        Trip.objects.filter(status__in=Trip.ACTIVE_ASSIGNMENT_STATUSES)
        .exclude(vehicle__isnull=True, driver__isnull=True)
        .select_related("vehicle", "driver", "client")
    )
    alerts.extend(_overlap_alerts(active_trips))
    alerts.extend(_maintenance_alerts(active_trips))
    alerts.extend(_license_alerts(active_trips))

    alerts.sort(key=lambda item: SEVERITY_ORDER[item["severity"]])
    return alerts


def filter_fields(request):
    """List-of-dicts shape identical to TripListView.get_context_data's
    pattern (apps/trips/views.py), restricted to the board's own statuses
    plus the two new "time" fields filter_bar.html now supports."""
    value = lambda name: request.GET.get(name, "").strip()  # noqa: E731
    return [
        {
            "type": "search", "name": "q", "label": "Search", "value": value("q"), "col": 3,
            "placeholder": "Trip ID, client, vehicle, driver, site…",
        },
        {
            "type": "select", "name": "status", "label": "Status", "value": value("status"), "col": 2,
            "choices": [(status.value, status.label) for status in BOARD_STATUSES],
        },
        {
            "type": "select", "name": "client", "label": "Client", "value": value("client"), "col": 2,
            "choices": [(str(c.id), c.client_name) for c in Client.objects.filter(status=Client.Status.ACTIVE)],
        },
        {
            "type": "select", "name": "priority", "label": "Priority", "value": value("priority"), "col": 1,
            "choices": Trip.Priority.choices,
        },
        {
            "type": "select", "name": "trip_type", "label": "Trip Type", "value": value("trip_type"), "col": 2,
            "choices": Trip.TripType.choices,
        },
        {
            "type": "select", "name": "vehicle_type", "label": "Vehicle Type", "value": value("vehicle_type"), "col": 2,
            "choices": [(str(vt.id), vt.name) for vt in VehicleType.objects.filter(is_active=True)],
        },
        {
            "type": "select", "name": "driver", "label": "Driver", "value": value("driver"), "col": 2,
            "choices": [
                (str(d.id), d.get_full_name())
                for d in Driver.objects.filter(employment_status=Driver.EmploymentStatus.ACTIVE)
            ],
        },
        {"type": "time", "name": "start_time_from", "label": "Start After", "value": value("start_time_from"), "col": 1},
        {"type": "time", "name": "start_time_to", "label": "Start Before", "value": value("start_time_to"), "col": 1},
    ]
