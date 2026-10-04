"""Dispatch Board view — an orchestration layer only.

Renders one page from apps.trips.dispatch_services' read-only queries and
lets the dispatcher act via the *existing* apps.trips lifecycle endpoints
(trip_assign/trip_dispatch/trip_start/trip_delay/trip_resume/trip_complete).
No new POST endpoints, no new validation, no new audit calls — everything
that mutates a Trip already lives in apps.trips.services and is reused
verbatim.
"""

import datetime
import logging

from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView

from apps.audit.models import AuditLog
from apps.core.permissions import ModulePermissionRequiredMixin, user_has_permission
from apps.core.scoping import is_client_scoped
from apps.trips import dispatch_services
from apps.trips.forms import TripAssignForm
from apps.trips.models import Trip

logger = logging.getLogger(__name__)

RECENT_ACTIVITY_ACTIONS = [
    AuditLog.Action.ASSIGN,
    AuditLog.Action.DISPATCH,
    AuditLog.Action.START,
    AuditLog.Action.DELAY,
    AuditLog.Action.RESUME,
    AuditLog.Action.COMPLETE,
]

# Only SCHEDULED trips show the "Assign Resources" action (ASSIGNED trips
# show "Dispatch" instead) — building TripAssignForm for every trip on the
# board would run its two scoped queries per instance for cards that can
# never show the button.
ASSIGNABLE_COLUMN_STATUSES = {Trip.Status.SCHEDULED}


def _parse_date(raw):
    if raw:
        try:
            return datetime.date.fromisoformat(raw)
        except ValueError:
            pass
    return timezone.now().date()


class DispatchBoardView(ModulePermissionRequiredMixin, TemplateView):
    """The dispatcher's single-page operational workspace — see
    apps.trips.dispatch_services for how each panel is computed."""

    template_name = "trips/dispatch_board.html"
    permission_module = "trip"
    permission_action = "view"

    def dispatch(self, request, *args, **kwargs):
        # Internal ops tool: its panels (available vehicles/drivers across
        # ALL clients) aren't client-scoped, so client users are refused.
        if request.user.is_authenticated and is_client_scoped(request.user):
            raise PermissionDenied("The Dispatch Board is for internal staff.")
        return super().dispatch(request, *args, **kwargs)

    def _safe(self, label, fn):
        """Isolates one panel's query from the rest of the page — copied
        verbatim from apps.core.views.DashboardView._safe so one panel's
        failure never blanks the board."""
        try:
            return fn(), False
        except Exception:
            logger.exception("Dispatch Board panel '%s' failed to load", label)
            return None, True

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        selected_date = _parse_date(self.request.GET.get("date", ""))
        today = timezone.now().date()

        board_qs = dispatch_services.board_queryset(selected_date, self.request.GET)

        columns, columns_error = self._safe("columns", lambda: dispatch_services.group_into_columns(board_qs))
        available_vehicles, vehicles_error = self._safe(
            "available_vehicles", lambda: list(dispatch_services.available_vehicles())
        )
        available_drivers, drivers_error = self._safe(
            "available_drivers", lambda: list(dispatch_services.available_drivers())
        )
        alerts, alerts_error = self._safe("dispatch_alerts", lambda: dispatch_services.dispatch_alerts(user))
        recent_activity, activity_error = self._safe(
            "recent_activity",
            lambda: list(
                AuditLog.objects.filter(module="trip", entity="Trip", action__in=RECENT_ACTIVITY_ACTIONS)
                .select_related("user")
                .order_by("-timestamp")[:20]
            ),
        )

        quick_filter_counts, quick_filter_counts_error = self._safe(
            "quick_filter_counts", lambda: dispatch_services.quick_filter_counts(selected_date, self.request.GET)
        )
        quick_filter_counts = quick_filter_counts or {}

        conflict_count = len([a for a in (alerts or []) if a.get("category") == "conflict"]) if not alerts_error else 0

        def _quick_filter_url(value):
            params = self.request.GET.copy()
            if value:
                params["quick_filter"] = value
            else:
                params.pop("quick_filter", None)
            query = params.urlencode()
            return f"{self.request.path}?{query}" if query else self.request.path

        # Attention Summary — a top-of-page glance strip. Deliberately built
        # from quick_filter_counts (every filter EXCEPT quick_filter), not
        # from `columns` (which IS quick_filter-scoped) — otherwise clicking
        # one pill would collapse the other tiles to 0 while the pill bar
        # right below correctly kept showing their real counts, the exact
        # mismatched-counts bug the redesign brief calls out.
        context["attention_summary"] = [
            {
                "key": "needs_assignment", "label": "Needs Assignment",
                "count": quick_filter_counts.get("needs_assignment", 0),
                "tone": "warning", "url": _quick_filter_url("needs_assignment"),
            },
            {
                "key": "conflicts", "label": "Conflicts", "count": conflict_count,
                "tone": "danger", "url": "#dispatchAttentionSection",
            },
            {
                "key": "ready", "label": "Ready to Dispatch", "count": quick_filter_counts.get("ready", 0),
                "tone": "info", "url": _quick_filter_url("ready"),
            },
            {
                "key": "delayed", "label": "Delayed", "count": quick_filter_counts.get("delayed", 0),
                "tone": "danger", "url": _quick_filter_url("delayed"),
            },
        ]

        can_act = user_has_permission(user, "trip", "update")
        if can_act and columns:
            # Annotated directly onto each trip (not a separate uuid-keyed
            # dict) so the card partial can just read `trip.assign_form` —
            # Django templates have no clean variable-key dict lookup.
            # Built only for assignable columns: TripAssignForm.__init__ runs
            # two scoped queries per instance, so this stays bounded to the
            # cards that can actually show the button.
            for column in columns:
                if column["status"] in ASSIGNABLE_COLUMN_STATUSES:
                    for trip in column["trips"]:
                        trip.assign_form = TripAssignForm(trip=trip)

        context.update(
            {
                "columns": columns or [],
                "columns_error": columns_error,
                "available_vehicles": available_vehicles or [],
                "available_vehicles_error": vehicles_error,
                "available_drivers": available_drivers or [],
                "available_drivers_error": drivers_error,
                "alerts": alerts or [],
                "alerts_error": alerts_error,
                "recent_activity": recent_activity or [],
                "recent_activity_error": activity_error,
                "can_act": can_act,
                "filter_fields": dispatch_services.filter_fields(self.request),
                "quick_filter": self.request.GET.get("quick_filter", ""),
                "quick_filter_counts": quick_filter_counts,
                "quick_filter_counts_error": quick_filter_counts_error,
                "selected_date": selected_date,
                "is_today": selected_date == today,
                "date_prev": (selected_date - datetime.timedelta(days=1)).isoformat(),
                "date_next": (selected_date + datetime.timedelta(days=1)).isoformat(),
                "date_today": today.isoformat(),
                "board_url": reverse("dispatch:board"),
            }
        )
        return context
