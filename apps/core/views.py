import datetime
import json
import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView

from apps.accounts.models import Role, User
from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.clients.models import Client
from apps.core.dashboard_services import DATE_RANGE_CHOICES, DashboardService, OperationsService
from apps.core.forms import SystemSettingsForm
from apps.core.models import SystemSettings
from apps.core.permissions import ModulePermissionRequiredMixin, has_admin_access, user_has_any_permission, user_has_permission
from apps.core.scoping import is_client_scoped
from apps.core.utils import model_to_dict_safe
from apps.documents.models import Document
from apps.drivers.models import Driver
from apps.locations.models import Branch, Site
from apps.maintenance.models import Maintenance
from apps.notifications import services as notification_services
from apps.notifications.models import Notification
from apps.tracking import services as tracking_services
from apps.trips.models import Trip
from apps.vehicles.models import Vehicle

logger = logging.getLogger(__name__)


def _safe_json(data):
    """json.dumps for embedding inside an inline <script>: a role/label value
    containing "</script>" must not be able to close the tag, so <, > and & are
    emitted as unicode escapes (still valid JSON, identical when parsed)."""
    return (json.dumps(data).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


VALID_ACTIVITY_RANGES = {7, 30, 90}

# Headline/subtitle per system role. Content of the workspace is decided by
# the viewer's actual permissions, not by this table — it only sets the tone
# for the page header. Custom roles fall back to "<Role name> workspace".
WORKSPACE_PROFILES = {
    "fleet-manager": ("Fleet Operations", "Fleet Manager workspace — vehicles, drivers, maintenance and GPS connectivity."),
    "operations-manager": ("Fleet Operations", "Operations Manager workspace — trips, routes, geofences and alerts."),
    "dispatcher": ("Fleet Operations", "Dispatcher workspace — today's trips and who is available to run them."),
    "client-admin": ("Fleet Operations", "Client Admin workspace — read-only visibility into your fleet activity."),
    "read-only": ("Fleet Operations", "Read-only workspace — a view across the modules you can access."),
}

DATE_RANGE_LABELS = {"today": "Today", "yesterday": "Yesterday", "week": "This Week", "month": "This Month"}


class TermsView(TemplateView):
    """Public — linked from the login page footer. No login required."""

    template_name = "legal/terms.html"


class PrivacyView(TemplateView):
    """Public — linked from the login page footer. No login required."""

    template_name = "legal/privacy.html"


class MasterDataView(LoginRequiredMixin, TemplateView):
    """Master Data hub — a navigation/summary workspace over Clients, Sites,
    and Branches.

    Each of those is already its own complete module (models, CRUD, search,
    export, permissions, audit); this page doesn't re-implement any of that.
    It only aggregates real counts and the most recently created records,
    then links out to the existing list/detail pages — same relationship
    the main Dashboard has to Vehicles/Trips/Maintenance. Visibility of each
    card is gated per-module exactly like the dashboard's widgets, so a
    viewer without "site" access sees no site numbers at all rather than a
    zero.
    """

    template_name = "core/master_data.html"

    def dispatch(self, request, *args, **kwargs):
        # Server-side gate: the hub is only for viewers of at least one of
        # its three modules (the sidebar hides it otherwise, but hiding a
        # link is not security — typing the URL must 403 too).
        if request.user.is_authenticated and (
            is_client_scoped(request.user)
            or not user_has_any_permission(request.user, ("client", "site", "branch"), "view")
        ):
            raise PermissionDenied("You do not have permission to view Master Data.")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        can_view_client = user_has_permission(user, "client", "view")
        can_view_site = user_has_permission(user, "site", "view")
        can_view_branch = user_has_permission(user, "branch", "view")

        recent_items = []
        if can_view_client:
            for c in Client.objects.order_by("-created_at")[:5]:
                recent_items.append(
                    {
                        "type": "Client", "icon": "building", "label": c.client_name,
                        "sub": c.client_code, "url": reverse("clients:client_detail", kwargs={"uuid": c.uuid}),
                        "created_at": c.created_at,
                    }
                )
        if can_view_site:
            for s in Site.objects.select_related("client").order_by("-created_at")[:5]:
                recent_items.append(
                    {
                        "type": "Site", "icon": "geo-alt", "label": s.site_name,
                        "sub": s.client.client_name, "url": reverse("locations:site_detail", kwargs={"uuid": s.uuid}),
                        "created_at": s.created_at,
                    }
                )
        if can_view_branch:
            for b in Branch.objects.order_by("-created_at")[:5]:
                recent_items.append(
                    {
                        "type": "Branch", "icon": "diagram-3", "label": b.name,
                        "sub": b.code, "url": reverse("locations:branch_detail", kwargs={"uuid": b.uuid}),
                        "created_at": b.created_at,
                    }
                )
        recent_items.sort(key=lambda item: item["created_at"], reverse=True)

        context.update(
            {
                "can_view_client": can_view_client,
                "can_view_site": can_view_site,
                "can_view_branch": can_view_branch,
                "has_any_master_data_access": can_view_client or can_view_site or can_view_branch,
                "client_count": Client.objects.count() if can_view_client else None,
                "active_client_count": Client.objects.filter(status=Client.Status.ACTIVE).count() if can_view_client else None,
                "site_count": Site.objects.count() if can_view_site else None,
                "active_site_count": Site.objects.filter(status=Site.Status.ACTIVE).count() if can_view_site else None,
                "branch_count": Branch.objects.count() if can_view_branch else None,
                "active_branch_count": Branch.objects.filter(status=Branch.Status.ACTIVE).count() if can_view_branch else None,
                "recent_items": recent_items[:8],
            }
        )
        return context


SETTINGS_AUDIT_FIELDS = [
    "company_name", "support_email", "support_phone", "default_timezone",
    "default_currency", "date_format", "document_expiry_warning_days", "maintenance_due_soon_days",
]


class SettingsView(ModulePermissionRequiredMixin, TemplateView):
    """System Settings — a single singleton row (``SystemSettings.load()``),
    not a ListView/UpdateView pair, since there's exactly one record and no
    list/create/delete lifecycle. ``view`` shows the form; saving requires
    ``update`` separately so a viewer-only admin can see current settings
    without being able to change them, same split the rest of the RBAC
    catalog uses everywhere else (view vs. update)."""

    template_name = "core/settings.html"
    permission_module = "settings"
    permission_action = "view"

    def get(self, request, *args, **kwargs):
        form = SystemSettingsForm(instance=SystemSettings.load())
        return self.render_to_response({"form": form, "can_update": user_has_permission(request.user, "settings", "update")})

    def post(self, request, *args, **kwargs):
        if not user_has_permission(request.user, "settings", "update"):
            raise PermissionDenied("You do not have permission to update settings.")
        instance = SystemSettings.load()
        old_value = model_to_dict_safe(instance, SETTINGS_AUDIT_FIELDS)
        form = SystemSettingsForm(request.POST, instance=instance)
        if form.is_valid():
            settings_obj = form.save(commit=False)
            settings_obj.updated_by = request.user
            settings_obj.save()
            log_action(
                action=AuditLog.Action.UPDATE, module="settings", entity="SystemSettings", entity_id="1",
                old_value=old_value, new_value=model_to_dict_safe(settings_obj, SETTINGS_AUDIT_FIELDS),
                user=request.user, request=request,
            )
            messages.success(request, "Settings were updated successfully.")
            return redirect("core:settings")
        return self.render_to_response({"form": form, "can_update": True})


class DashboardView(LoginRequiredMixin, TemplateView):
    """Fleet Operations command center — the landing page after login.

    Real-time fleet/driver state and date-scoped trip activity are two
    different kinds of data (see apps.core.dashboard_services) and are
    fetched separately so the date-range selector can never accidentally
    affect "how many vehicles are on trip right now".

    Every module-specific section is computed through OperationsService/
    DashboardService, both of which check the viewer's permission before
    returning anything — a user without "trip" view access gets ``None``
    for every trip-shaped key, not a zero, so the template can tell
    "nothing to show" apart from "not allowed to know" and hide the
    section entirely rather than leak a restricted count.
    """

    def get_template_names(self):
        # The admin dashboard is earned by holding view access to any
        # administration module (see apps.core.permissions.has_admin_access),
        # not by a hardcoded role name. Everyone else gets the role-based
        # workspace, which only ever contains widgets for modules they hold.
        if has_admin_access(self.request.user):
            return ["dashboard/admin.html"]
        return ["dashboard/workspace.html"]

    def _safe(self, label, fn):
        """Isolates one widget's query from the rest of the page — a bug or
        transient DB issue in one section must not blank the whole
        dashboard (per the "if one widget fails" requirement)."""
        try:
            return fn(), False
        except Exception:
            logger.exception("Dashboard widget '%s' failed to load", label)
            return None, True

    @staticmethod
    def _greeting(now):
        hour = timezone.localtime(now).hour
        if hour < 12:
            return "Good morning"
        if hour < 17:
            return "Good afternoon"
        return "Good evening"

    @staticmethod
    def _workspace_profile(user):
        role = getattr(user, "role", None)
        if role is None or not role.is_active:
            return "Fleet Operations", "No active role is assigned to your account yet."
        if role.code in WORKSPACE_PROFILES:
            return WORKSPACE_PROFILES[role.code]
        return f"{role.name} workspace", "An overview of the modules your role can access."

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        now = timezone.now()
        today = now.date()
        is_admin = has_admin_access(user)

        try:
            activity_range = int(self.request.GET.get("range", 30))
        except (TypeError, ValueError):
            activity_range = 30
        if activity_range not in VALID_ACTIVITY_RANGES:
            activity_range = 30

        date_range = self.request.GET.get("date_range", "today")
        if date_range not in DATE_RANGE_CHOICES:
            date_range = "today"

        ops = OperationsService(user)
        dash = DashboardService(user, date_range=date_range)

        can_view_vehicle = ops.can_view("vehicle")
        can_view_driver = ops.can_view("driver")
        can_view_trip = ops.can_view("trip")
        can_view_maintenance = ops.can_view("maintenance")
        can_view_document = ops.can_view("document")
        can_view_client = ops.can_view("client")
        can_view_site = ops.can_view("site")
        can_view_user = ops.can_view("user")
        can_view_role = ops.can_view("role")
        can_view_audit = ops.can_view("audit_log")
        can_view_alert = ops.can_view("alert")
        can_view_tracking = ops.can_view("tracking_device")

        # ---- Administration widgets: each gated by its OWN module, so an ----
        # ---- auditor (audit_log only) never sees user/role numbers.     ----
        status_counts = {}
        users_trend_pct = None
        users_trend_direction = ""
        role_counts = []
        if can_view_user:
            status_counts = dict(User.objects.values_list("status").annotate(count=Count("id")))
            window_30d_start = now - timezone.timedelta(days=30)
            window_prev_30d_start = now - timezone.timedelta(days=60)
            users_this_window = User.all_objects.filter(created_at__gte=window_30d_start).count()
            users_prev_window = User.all_objects.filter(
                created_at__gte=window_prev_30d_start, created_at__lt=window_30d_start
            ).count()
            if users_prev_window:
                users_trend_pct = round((users_this_window - users_prev_window) / users_prev_window * 100, 1)
                users_trend_direction = "up" if users_trend_pct >= 0 else "down"
        if can_view_role:
            role_counts = list(
                Role.objects.filter(is_active=True)
                .annotate(user_count=Count("users"))
                .order_by("-user_count")
                .values("name", "user_count")
            )
        audit_today_count = logins_today_count = None
        if can_view_audit:
            audit_today_count = AuditLog.objects.filter(timestamp__date=today).count()
            logins_today_count = AuditLog.objects.filter(timestamp__date=today, action=AuditLog.Action.LOGIN).count()

        fleet_status, fleet_status_error = self._safe("fleet_status", ops.fleet_status)
        driver_availability, driver_availability_error = self._safe("driver_availability", ops.driver_availability)
        maintenance_overview, maintenance_overview_error = self._safe("maintenance_overview", ops.maintenance_overview)
        document_compliance, document_compliance_error = self._safe("document_compliance", ops.document_compliance)
        client_operations, client_operations_error = self._safe("client_operations", ops.client_operations)

        trip_status_summary, trip_status_summary_error = self._safe("trip_status_summary", dash.trip_status_summary)
        active_trips_board, active_trips_board_error = self._safe("active_trips_board", dash.active_trips_board)
        recent_trips, recent_trips_error = self._safe("recent_trips", dash.recent_trips)
        site_operations, site_operations_error = self._safe("site_operations", dash.site_operations)
        trip_trend, _trip_trend_error = self._safe("trip_trend", dash.trip_trend)

        # Account-hygiene signals (locked/suspended/no-role users) are user
        # administration data — only surfaced to viewers who hold user.view.
        hygiene = self._account_hygiene_alerts() if can_view_user else []
        attention_items, attention_error = self._safe("attention_items", lambda: ops.attention_items(hygiene))

        total_vehicles = fleet_status["total"] if fleet_status else None
        fleet_status_json = _safe_json({"labels": [], "data": []})
        if can_view_vehicle and fleet_status:
            vehicle_status_counts = fleet_status["status_counts"]
            fleet_status_json = _safe_json(
                {
                    "labels": [Vehicle.Status(code).label for code in vehicle_status_counts.keys()],
                    "data": list(vehicle_status_counts.values()),
                }
            )

        # Gated on tracking_device/view independently of vehicle access,
        # since GPS connectivity is a tracking concept, not a fleet-CRUD one.
        fleet_connectivity = None
        if can_view_tracking:
            fleet_connectivity = tracking_services.fleet_connectivity_counts(user)

        trip_status_counts = {}
        total_trips = 0
        if can_view_trip:
            trip_status_counts = dict(ops.trips().values_list("status").annotate(count=Count("id")))
            total_trips = ops.trips().count()

        maintenance_scheduled_count = maintenance_due_today_count = maintenance_overdue_count = 0
        maintenance_in_progress_count = maintenance_completed_this_month = 0
        maintenance_cost_this_month = 0
        if can_view_maintenance:
            maintenance_scheduled_count = ops.maintenance().filter(status=Maintenance.Status.SCHEDULED).count()
            maintenance_due_today_count = ops.maintenance().due_today().count()
            maintenance_overdue_count = ops.maintenance().overdue().count()
            maintenance_in_progress_count = ops.maintenance().filter(status=Maintenance.Status.IN_PROGRESS).count()
            maintenance_completed_this_month = ops.maintenance().filter(
                status=Maintenance.Status.COMPLETED, completed_date__year=now.year, completed_date__month=now.month,
            ).count()
            maintenance_cost_this_month = ops.maintenance().filter(
                status=Maintenance.Status.COMPLETED, completed_date__year=now.year, completed_date__month=now.month,
            ).aggregate(total=Sum("actual_cost"))["total"] or 0

        documents_total_count = documents_expiring_soon_count = documents_expired_count = 0
        documents_uploaded_this_month = 0
        if can_view_document:
            documents_total_count = ops.documents().count()
            documents_expiring_soon_count = ops.documents().expiring_soon().count()
            documents_expired_count = ops.documents().expired().count()
            documents_uploaded_this_month = ops.documents().filter(
                created_at__year=now.year, created_at__month=now.month
            ).count()

        # ---- Activity feed: the global audit trail only for audit_log ----
        # ---- viewers; everyone else sees just their own actions.       ----
        if can_view_audit:
            recent_activity = AuditLog.objects.select_related("user").order_by("-timestamp")[:8]
            activity_scope = "all"
        else:
            recent_activity = AuditLog.objects.filter(user=user).order_by("-timestamp")[:8]
            activity_scope = "mine"

        recent_notifications = list(Notification.objects.filter(recipient=user, is_archived=False)[:5])
        unread_notifications = notification_services.unread_count(user)

        trip_status_json = _safe_json({"labels": [], "data": []})
        if trip_status_summary:
            keys = ["scheduled", "assigned", "dispatched", "in_progress", "completed", "delayed", "cancelled"]
            trip_status_json = _safe_json(
                {"labels": [k.replace("_", " ").title() for k in keys], "data": [trip_status_summary[k] for k in keys]}
            )

        headline, subtitle = self._workspace_profile(user)
        has_any_widget = any(
            [can_view_vehicle, can_view_driver, can_view_trip, can_view_maintenance, can_view_document,
             can_view_client, can_view_site, can_view_tracking, can_view_alert]
        )

        context.update(
            {
                "is_admin_dashboard": is_admin,
                "is_client_user": is_client_scoped(user),
                "delayed_trips_url": ops.dispatch_url("delayed", "DELAYED"),
                "greeting": self._greeting(now),
                "workspace_headline": headline,
                "workspace_subtitle": subtitle,
                "has_any_widget": has_any_widget,
                "date_range": date_range,
                "date_range_label": DATE_RANGE_LABELS[date_range],
                "can_view_vehicle": can_view_vehicle,
                "can_view_driver": can_view_driver,
                "can_view_trip": can_view_trip,
                "can_view_maintenance": can_view_maintenance,
                "can_view_document": can_view_document,
                "can_view_client": can_view_client,
                "can_view_site": can_view_site,
                "can_view_user": can_view_user,
                "can_view_role": can_view_role,
                "can_view_audit": can_view_audit,
                "can_view_alert": can_view_alert,
                "can_view_tracking": can_view_tracking,
                "fleet_status": fleet_status,
                "fleet_status_error": fleet_status_error,
                "driver_availability": driver_availability,
                "driver_availability_error": driver_availability_error,
                "maintenance_overview": maintenance_overview,
                "maintenance_overview_error": maintenance_overview_error,
                "document_compliance": document_compliance,
                "document_compliance_error": document_compliance_error,
                "client_operations": client_operations,
                "client_operations_error": client_operations_error,
                "trip_status_summary": trip_status_summary,
                "trip_status_summary_error": trip_status_summary_error,
                "active_trips_board": active_trips_board,
                "active_trips_board_error": active_trips_board_error,
                "recent_trips": recent_trips,
                "recent_trips_error": recent_trips_error,
                "site_operations": site_operations,
                "site_operations_error": site_operations_error,
                "attention_items": attention_items or [],
                "attention_error": attention_error,
                "total_vehicles": total_vehicles,
                "active_vehicles": fleet_status["status_counts"].get(Vehicle.Status.ACTIVE, 0) if fleet_status else None,
                "vehicles_on_trip": fleet_status["on_trip"] if fleet_status else None,
                "available_vehicles": fleet_status["available"] if fleet_status else None,
                "active_drivers": ops.drivers().filter(employment_status=Driver.EmploymentStatus.ACTIVE).count() if can_view_driver else None,
                "fleet_connectivity": fleet_connectivity,
                "fleet_status_json": fleet_status_json,
                "total_clients": ops.clients().count() if can_view_client else None,
                "active_clients": ops.clients().filter(status=Client.Status.ACTIVE).count() if can_view_client else None,
                "total_sites": ops.sites().count() if can_view_site else None,
                "total_trips": total_trips,
                "maintenance_scheduled_count": maintenance_scheduled_count,
                "maintenance_due_today_count": maintenance_due_today_count,
                "maintenance_overdue_count": maintenance_overdue_count,
                "maintenance_in_progress_count": maintenance_in_progress_count,
                "maintenance_completed_this_month": maintenance_completed_this_month,
                "maintenance_cost_this_month": maintenance_cost_this_month,
                "documents_total_count": documents_total_count,
                "documents_expiring_soon_count": documents_expiring_soon_count,
                "documents_expired_count": documents_expired_count,
                "documents_uploaded_this_month": documents_uploaded_this_month,
                "trips_in_progress": trip_status_counts.get(Trip.Status.IN_PROGRESS, 0) if can_view_trip else None,
                "trips_completed": trip_status_counts.get(Trip.Status.COMPLETED, 0) if can_view_trip else None,
                "trips_delayed": trip_status_counts.get(Trip.Status.DELAYED, 0) if can_view_trip else None,
                "total_users": User.objects.count() if can_view_user else None,
                "active_users": status_counts.get(User.Status.ACTIVE, 0) if can_view_user else None,
                "inactive_users": status_counts.get(User.Status.INACTIVE, 0) if can_view_user else None,
                "suspended_users": status_counts.get(User.Status.SUSPENDED, 0) if can_view_user else None,
                "locked_users": status_counts.get(User.Status.LOCKED, 0) if can_view_user else None,
                "invited_users": status_counts.get(User.Status.INVITED, 0) if can_view_user else None,
                "total_roles": Role.objects.filter(is_active=True).count() if can_view_role else None,
                "users_trend_pct": users_trend_pct,
                "users_trend_direction": users_trend_direction,
                "audit_today_count": audit_today_count,
                "logins_today_count": logins_today_count,
                "recent_activity": recent_activity,
                "activity_scope": activity_scope,
                "recent_notifications": recent_notifications,
                "unread_notifications": unread_notifications,
                "trip_status_json": trip_status_json,
                "trip_trend_json": _safe_json(trip_trend or {"labels": [], "total": [], "completed": []}),
                "users_by_status_json": _safe_json(
                    {
                        "labels": [User.Status(code).label for code in status_counts.keys()],
                        "data": list(status_counts.values()),
                    }
                ),
                "users_by_role_json": _safe_json(
                    {
                        "labels": [row["name"] for row in role_counts],
                        "data": [row["user_count"] for row in role_counts],
                    }
                ),
                "activity_range": activity_range,
                "activity_trend_json": _safe_json(
                    self._activity_trend(now, activity_range)
                    if can_view_audit
                    else {"labels": [], "activity": [], "logins": []}
                ),
            }
        )
        return context

    def _activity_trend(self, now, days):
        """Real per-day counts of audit activity and logins over the selected
        window — powers the Activity Trend chart with genuinely dynamic data."""
        start = (now - timezone.timedelta(days=days - 1)).date()
        end = now.date()

        def counts_by_day(queryset):
            rows = (
                queryset.filter(timestamp__date__gte=start, timestamp__date__lte=end)
                .annotate(day=TruncDate("timestamp"))
                .values("day")
                .annotate(count=Count("id"))
            )
            return {row["day"]: row["count"] for row in rows}

        all_by_day = counts_by_day(AuditLog.objects.all())
        logins_by_day = counts_by_day(AuditLog.objects.filter(action=AuditLog.Action.LOGIN))

        labels, activity_series, login_series = [], [], []
        cursor = start
        while cursor <= end:
            labels.append(cursor.strftime("%b %d"))
            activity_series.append(all_by_day.get(cursor, 0))
            login_series.append(logins_by_day.get(cursor, 0))
            cursor += timezone.timedelta(days=1)

        return {"labels": labels, "activity": activity_series, "logins": login_series}

    def _account_hygiene_alerts(self):
        """Account security/access signals (locked/suspended/no-role/stale
        invites) — always visible to any authenticated user regardless of
        fleet-module permissions, since these are about the portal itself,
        not fleet data. Folded into OperationsService.attention_items()
        alongside the new operational alerts for one unified panel."""
        items = []

        locked_count = User.objects.filter(status=User.Status.LOCKED).count()
        if locked_count:
            items.append(
                {
                    "severity": "critical",
                    "icon": "lock",
                    "title": f"{locked_count} account{'s' if locked_count != 1 else ''} locked out",
                    "description": "Locked accounts can't sign in until an administrator unlocks them.",
                    "url": reverse("accounts_portal:user_list") + "?status=LOCKED",
                }
            )

        suspended_count = User.objects.filter(status=User.Status.SUSPENDED).count()
        if suspended_count:
            items.append(
                {
                    "severity": "high",
                    "icon": "person-x",
                    "title": f"{suspended_count} account{'s' if suspended_count != 1 else ''} suspended",
                    "description": "Review suspended accounts to confirm they should stay off the platform.",
                    "url": reverse("accounts_portal:user_list") + "?status=SUSPENDED",
                }
            )

        no_role_count = User.objects.filter(role__isnull=True).count()
        if no_role_count:
            items.append(
                {
                    "severity": "medium",
                    "icon": "shield-exclamation",
                    "title": f"{no_role_count} user{'s' if no_role_count != 1 else ''} without a role",
                    "description": "Users with no role assigned can't access any module — assign one to restore access.",
                    "url": reverse("accounts_portal:user_list"),
                }
            )

        stale_cutoff = timezone.now() - timezone.timedelta(days=14)
        stale_invites = User.objects.filter(status=User.Status.INVITED, created_at__lt=stale_cutoff).count()
        if stale_invites:
            items.append(
                {
                    "severity": "low",
                    "icon": "envelope-exclamation",
                    "title": f"{stale_invites} invitation{'s' if stale_invites != 1 else ''} pending 14+ days",
                    "description": "These accounts were invited over two weeks ago and haven't been activated.",
                    "url": reverse("accounts_portal:user_list") + "?status=INVITED",
                }
            )

        return items
