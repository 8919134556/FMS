from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import ListView, TemplateView

from apps.alerts import report, services
from apps.alerts.models import Alert
from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.core.models import SystemSettings
from apps.core.permissions import ModulePermissionRequiredMixin, module_permission_required, user_has_permission
from apps.core.scoping import scope_queryset
from apps.tracking import trip_report


def _visible_alerts(user):
    """Alert rows ``user`` may act on: client users only their own client's
    (vehicle) alerts; operational alerts have no client and stay staff-only."""
    return scope_queryset(Alert.objects.all(), user, "client_id")


class AlertReportView(ModulePermissionRequiredMixin, TemplateView):
    """Reports > Alert Report — the shell; rows, counts, details and downloads
    come from the RBAC'd, client-scoped API (apps.alerts.api_views)."""

    template_name = "alerts/alert_report.html"
    permission_module = "alert"
    permission_action = "view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["display_timezone"] = SystemSettings.load().default_timezone or "UTC"
        context["max_custom_range_days"] = trip_report.MAX_CUSTOM_RANGE_DAYS
        context["vehicles"] = list(report.report_vehicles(self.request.user).values("uuid", "registration_number"))
        context["alert_types"] = report.TYPE_CHOICES
        context["levels"] = report.LEVEL_CHOICES
        context["geofences"] = list(report.report_geofences(self.request.user).values("uuid", "name"))
        context["page_sizes"] = report.PAGE_SIZES
        context["can_update"] = user_has_permission(self.request.user, "alert", "update")
        return context


class AlertListView(ModulePermissionRequiredMixin, ListView):
    """Re-syncs from live data (see apps.alerts.services.sync_alerts) on
    every visit before listing, so this page is never stale even without a
    scheduler — the same "compute on read" approach Maintenance.objects.
    overdue()/Document.objects.expiring_soon() already use, just persisted
    here so it can carry a status workflow and an audit trail."""

    model = Alert
    template_name = "alerts/alert_list.html"
    context_object_name = "alerts"
    paginate_by = 25
    permission_module = "alert"
    permission_action = "view"

    def get_queryset(self):
        services.sync_alerts()
        qs = _visible_alerts(self.request.user).filter(is_archived=False).select_related("content_type")

        status = self.request.GET.get("status", "")
        if status:
            qs = qs.filter(status=status)

        severity = self.request.GET.get("severity", "")
        if severity:
            qs = qs.filter(severity=severity)

        category = self.request.GET.get("category", "")
        if category:
            qs = qs.filter(category=category)

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        status = self.request.GET.get("status", "")
        severity = self.request.GET.get("severity", "")
        category = self.request.GET.get("category", "")
        context["current_filters"] = {"status": status, "severity": severity, "category": category}
        context["filter_fields"] = [
            {"type": "select", "name": "status", "label": "Status", "value": status, "col": 3,
             "choices": Alert.Status.choices},
            {"type": "select", "name": "severity", "label": "Severity", "value": severity, "col": 3,
             "choices": Alert.Severity.choices},
            {"type": "select", "name": "category", "label": "Category", "value": category, "col": 3,
             "choices": Alert.Category.choices},
        ]
        visible = _visible_alerts(self.request.user)
        context["open_count"] = visible.filter(status=Alert.Status.OPEN, is_archived=False).count()
        context["acknowledged_count"] = visible.filter(status=Alert.Status.ACKNOWLEDGED, is_archived=False).count()
        context["critical_count"] = visible.filter(
            severity=Alert.Severity.CRITICAL, status__in=[Alert.Status.OPEN, Alert.Status.ACKNOWLEDGED], is_archived=False
        ).count()
        return context


@require_POST
@module_permission_required("alert", "update")
def acknowledge(request, uuid):
    alert = get_object_or_404(_visible_alerts(request.user), uuid=uuid)
    old_status = alert.status
    alert.status = Alert.Status.ACKNOWLEDGED
    alert.acknowledged_by = request.user
    alert.acknowledged_at = timezone.now()
    alert.save(update_fields=["status", "acknowledged_by", "acknowledged_at"])
    log_action(
        action=AuditLog.Action.UPDATE, module="alert", entity="Alert", entity_id=str(alert.pk),
        old_value={"status": old_status}, new_value={"status": Alert.Status.ACKNOWLEDGED},
        user=request.user, request=request,
    )
    if _wants_json(request):
        return JsonResponse({"ok": True, "status": alert.status})
    messages.success(request, "Alert acknowledged.")
    return redirect(request.POST.get("next") or reverse("alerts:alert_list"))


@require_POST
@module_permission_required("alert", "update")
def resolve(request, uuid):
    alert = get_object_or_404(_visible_alerts(request.user), uuid=uuid)
    old_status = alert.status
    alert.status = Alert.Status.RESOLVED
    alert.resolved_by = request.user
    alert.resolved_at = timezone.now()
    alert.save(update_fields=["status", "resolved_by", "resolved_at"])
    log_action(
        action=AuditLog.Action.UPDATE, module="alert", entity="Alert", entity_id=str(alert.pk),
        old_value={"status": old_status}, new_value={"status": Alert.Status.RESOLVED},
        user=request.user, request=request,
    )
    if _wants_json(request):
        return JsonResponse({"ok": True, "status": alert.status})
    messages.success(request, "Alert resolved.")
    return redirect(request.POST.get("next") or reverse("alerts:alert_list"))


@require_POST
@module_permission_required("alert", "archive")
def archive(request, uuid):
    alert = get_object_or_404(_visible_alerts(request.user), uuid=uuid)
    alert.is_archived = True
    alert.save(update_fields=["is_archived"])
    log_action(
        action=AuditLog.Action.ARCHIVE, module="alert", entity="Alert", entity_id=str(alert.pk),
        user=request.user, request=request,
    )
    messages.success(request, "Alert archived.")
    return redirect(request.POST.get("next") or reverse("alerts:alert_list"))


def _wants_json(request):
    """The Alert Report acknowledges/resolves in place (fetch); the Alerts list posts forms."""
    return request.headers.get("Accept") == "application/json"
