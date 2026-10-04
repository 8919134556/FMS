"""Alert Report JSON + downloads — mounted at ``/api/v1/alerts/``. RBAC: the
``alert`` module (view); rows are client-scoped in apps.alerts.report."""

import logging

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.alerts import exports
from apps.alerts import report as ar
from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.core.api_permissions import HasModulePermission
from apps.core.permissions import user_has_permission
from apps.tracking.api_views import EXPORT_TYPES, TripReportExportRateThrottle, _file_response, _int_param, _report_error
from apps.tracking.authentication import FleetReadRateThrottle
from apps.tracking.trip_analytics import ReportError

logger = logging.getLogger(__name__)


def _selection(request):
    return ar.resolve_selection(
        user=request.user, vehicle=request.GET.get("vehicle", ""), alert_type=request.GET.get("alert_type", ""),
        status=request.GET.get("status", ""), level=request.GET.get("level", ""),
        geofence=request.GET.get("geofence", ""),
        range_key=request.GET.get("range", "today"),
        from_str=request.GET.get("from", ""), to_str=request.GET.get("to", ""),
    )


class AlertReportDataView(APIView):
    """GET /api/v1/alerts/report/?range=…[&from&to]&vehicle=<uuid|all>&alert_type=&level=&geofence=<uuid>&status=
    &page=&page_size=&sort=time|vehicle|type|level|status|speed|voltage&dir=asc|desc"""

    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "alert"
    action = "list"
    throttle_classes = [FleetReadRateThrottle]  # auto-refreshed like the other live reports

    def get(self, request):
        try:
            selection = _selection(request)
        except ReportError as error:
            return _report_error(error.message, error.status)
        result = ar.page(
            selection, page_number=_int_param(request, "page", 1),
            page_size=_int_param(request, "page_size", ar.DEFAULT_PAGE_SIZE),
            sort=request.GET.get("sort", "time"), direction=request.GET.get("dir", "desc"),
        )
        return Response({
            "selection": {
                "vehicle": str(selection.vehicle.uuid) if selection.vehicle else "all",
                "vehicle_label": selection.vehicle_label,
                "type": selection.alert_type, "level": selection.level, "status": selection.status,
                "geofence": str(selection.geofence.uuid) if selection.geofence else "",
                "range": {"key": selection.range_key, "start": selection.start_date.isoformat(),
                          "end": selection.end_date.isoformat()},
            },
            "summary": ar.summary(selection),
            "alerts": {**{k: result[k] for k in ("total", "page", "pages", "page_size")},
                       "rows": [ar.serialize(a) for a in result["rows"]]},
            "can_update": user_has_permission(request.user, "alert", "update"),
        })


class AlertDetailView(APIView):
    """GET /api/v1/alerts/<uuid>/ — one alert the user may see (404 otherwise),
    for the detail popup opened from a notification."""

    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "alert"
    action = "retrieve"
    throttle_classes = [FleetReadRateThrottle]

    def get(self, request, uuid):
        alert = ar.scoped_alerts(request.user).select_related(
            "vehicle", "driver", "client", "acknowledged_by", "resolved_by").prefetch_related(
            "content_object").filter(uuid=uuid).first()
        if alert is None:
            return _report_error("This alert was not found or you don't have access to it.", 404)
        return Response({"alert": ar.serialize(alert),
                         "can_update": user_has_permission(request.user, "alert", "update")})


class AlertReportExportView(APIView):
    """GET /api/v1/alerts/report/export/?type=pdf|xlsx + the report's filters —
    every matching alert, not just the page on screen."""

    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "alert"
    action = "list"
    throttle_classes = [TripReportExportRateThrottle]

    def get(self, request):
        file_type = request.GET.get("type", "").strip().lower()
        if file_type not in EXPORT_TYPES:
            return _report_error("Choose a download type: PDF or Excel.", 400)
        try:
            selection = _selection(request)
            count = ar.alerts_queryset(selection).count()
            if not count:
                raise ReportError("No alerts were found for the selected filters and period.", status=404)
            render = exports.alert_report_pdf if file_type == "pdf" else exports.alert_report_xlsx
            content = render(selection)
        except ReportError as error:
            return _report_error(error.message, error.status)
        except Exception:
            logger.exception("Alert report export failed")
            return _report_error("The report could not be generated. Please try again, or choose a shorter range.", 500)

        log_action(
            action=AuditLog.Action.EXPORT, module="alert", entity="AlertReport",
            entity_id=f"{selection.start_date}..{selection.end_date}",
            new_value={"type": file_type, "alerts": count, "vehicle": selection.vehicle_label,
                       "alert_type": selection.alert_type or "all", "level": selection.level or "all",
                       "status": selection.status or "all"},
            user=request.user, request=request,
        )
        name = selection.vehicle.registration_number if selection.vehicle else "all-vehicles"
        filename = f"alert-report_{name}_{selection.start_date:%Y%m%d}"
        if selection.end_date != selection.start_date:
            filename += f"-{selection.end_date:%Y%m%d}"
        return _file_response(content, file_type, filename)
