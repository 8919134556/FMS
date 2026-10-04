import logging

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from apps.audit.models import AuditLog
from apps.audit.services import log_action

from apps.core.api_permissions import HasModulePermission
from apps.core.scoping import scope_queryset
from apps.core.utils import display_timezone
from apps.tracking import (
    location_exports,
    location_report,
    odometer_exports,
    odometer_report,
    services,
    trip_analytics,
    trip_exports,
    trip_report,
)
from apps.tracking.authentication import (
    DeviceIngestRateThrottle,
    DeviceKeyAuthentication,
    FleetReadRateThrottle,
    IsActiveDevice,
)
from apps.tracking.models import VehicleCurrentTelemetry
from apps.tracking.serializers import (
    FleetCurrentTelemetrySerializer,
    TelemetryCurrentSerializer,
    TelemetryHistorySerializer,
    TripReportSerializer,
    TripRouteSerializer,
)
from apps.tracking.services import TelemetryIngestionService, connection_status_for
from apps.vehicles.models import Vehicle

logger = logging.getLogger(__name__)

# Single-endpoint concerns — module-level constants, mirroring
# apps.core.dashboard_services.LIVE_BOARD_LIMIT rather than settings.py.
TELEMETRY_HISTORY_DEFAULT_LIMIT = 100
TELEMETRY_HISTORY_HARD_CAP = 1000


class TelemetryIngestView(APIView):
    """POST /api/telematics/ingest/ — device-authenticated, not RBAC'd.
    Accepts a single event object or ``{"events": [...]}``. Thin: all logic
    lives in TelemetryIngestionService."""

    authentication_classes = [DeviceKeyAuthentication]
    permission_classes = [IsActiveDevice]
    throttle_classes = [DeviceIngestRateThrottle]

    def post(self, request):
        if not isinstance(request.data, dict):
            return Response({"detail": "Malformed payload — expected a JSON object."}, status=400)

        result = TelemetryIngestionService.ingest(device=request.auth, raw_payload=request.data)
        if result.accepted and not result.rejected:
            status_code = 201
        elif result.accepted:
            status_code = 202
        else:
            status_code = 400
        return Response(
            {"accepted": result.accepted, "rejected": result.rejected, "errors": result.errors}, status=status_code
        )


class VehicleCurrentTelemetryView(APIView):
    """GET /api/v1/tracking/vehicles/<uuid>/telemetry/current/ — human,
    session/RBAC-authenticated (module ``tracking_device``, action
    ``view``), reusing the existing HasModulePermission exactly as
    apps.accounts.api_views does for its ViewSets. ``action = "retrieve"``
    is set below purely so HasModulePermission's viewset-shaped action
    lookup (list/retrieve -> view) also works on this plain APIView."""

    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "tracking_device"
    action = "retrieve"

    def get(self, request, vehicle_uuid):
        vehicle = get_object_or_404(scope_queryset(Vehicle.objects.all(), request.user), uuid=vehicle_uuid)
        try:
            current = vehicle.current_telemetry
        except VehicleCurrentTelemetry.DoesNotExist:
            current = None
        status = connection_status_for(current)
        if current is None:
            return Response({"connection_status": status, "detail": "No telemetry available."}, status=200)
        serializer = TelemetryCurrentSerializer(current, context={"connection_status": status})
        return Response(serializer.data)


class VehicleTelemetryHistoryView(APIView):
    """GET /api/v1/tracking/vehicles/<uuid>/telemetry/history/?from=&to=&limit=
    Human, session/RBAC-authenticated. Never returns unlimited history —
    ``limit`` is capped at TELEMETRY_HISTORY_HARD_CAP regardless of what's
    requested."""

    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "tracking_device"
    action = "retrieve"

    def get(self, request, vehicle_uuid):
        vehicle = get_object_or_404(scope_queryset(Vehicle.objects.all(), request.user), uuid=vehicle_uuid)
        # History is filtered by the client stamped on each reading at
        # ingest time, so a vehicle that later moved to another client never
        # shows its previous owner's history to the new one.
        qs = scope_queryset(vehicle.telemetry_events.all(), request.user)

        date_from = parse_datetime(request.query_params.get("from", "") or "")
        if date_from:
            qs = qs.filter(timestamp__gte=date_from)
        date_to = parse_datetime(request.query_params.get("to", "") or "")
        if date_to:
            qs = qs.filter(timestamp__lte=date_to)

        try:
            limit = int(request.query_params.get("limit", TELEMETRY_HISTORY_DEFAULT_LIMIT))
        except (TypeError, ValueError):
            limit = TELEMETRY_HISTORY_DEFAULT_LIMIT
        limit = max(1, min(limit, TELEMETRY_HISTORY_HARD_CAP))

        events = qs.order_by("-timestamp")[:limit]
        serializer = TelemetryHistorySerializer(events, many=True)
        return Response({"count": len(serializer.data), "limit": limit, "results": serializer.data})


class FleetCurrentTelemetryView(APIView):
    """GET /api/v1/tracking/fleet/current/ — one efficient call for the
    whole Live Fleet Map (Phase 3.4). Only vehicles with a
    VehicleCurrentTelemetry row are included — never a fabricated position.
    Same RBAC as the per-vehicle telemetry endpoints above."""

    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "tracking_device"
    action = "list"  # genuinely returns a list; _ACTION_MAP maps list -> view
    throttle_classes = [FleetReadRateThrottle]  # polled every 5 s by the map; see the class docstring

    def get(self, request):
        results = services.fleet_current_telemetry(request.user)
        serializer = FleetCurrentTelemetrySerializer(results, many=True)
        return Response({
            "count": len(results),
            "results": serializer.data,
            # Health of the feed that keeps these rows fresh (None = not applicable).
            "sync": services.comms_sync_status(),
        })


class TripReportDataView(APIView):
    """GET /api/v1/tracking/trip-report/ — ignition-derived trips (see
    apps.tracking.trip_report for the detection rule and why it's computed
    on the fly rather than stored). Same RBAC as Live Tracking/GPS Devices:
    module ``tracking_device``, so a client user sees only their own fleet's
    trips, read-only, exactly as everywhere else telemetry is exposed.

    Query params: ``range`` (today/yesterday/last3/last5/last7/custom, default
    today), ``from``/``to`` (custom range, YYYY-MM-DD), ``vehicle`` (uuid),
    ``q`` (search registration number / driver name).
    """

    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "tracking_device"
    action = "list"
    throttle_classes = [FleetReadRateThrottle]  # same budget as the fleet feed; polled the same way

    def get(self, request):
        now = timezone.now()
        tz = display_timezone()
        range_key = request.GET.get("range", "today").strip().lower()
        start_date, end_date = trip_report.resolve_date_range(
            range_key, request.GET.get("from"), request.GET.get("to"), now=now, tz=tz
        )
        trips, summary = trip_report.compute_vehicle_trips(
            user=request.user,
            start_date=start_date,
            end_date=end_date,
            vehicle_uuid=request.GET.get("vehicle", "").strip(),
            search=request.GET.get("q", ""),
            now=now,
        )
        serializer = TripReportSerializer(trip_report.serialize_trips(trips, now=now), many=True)
        return Response(
            {
                "range": {"key": range_key, "start": start_date.isoformat(), "end": end_date.isoformat()},
                "summary": {
                    "total_trips": summary["total_trips"],
                    "active_trips": summary["active_trips"],
                    "total_distance_km": summary["total_distance_km"],
                    "avg_duration_seconds": summary["avg_duration_seconds"],
                },
                "count": len(trips),
                "results": serializer.data,
            }
        )


class TripRouteView(APIView):
    """GET /api/v1/tracking/trip-report/route/ — the GPS points for ONE trip
    (the Map column's thumbnail/expanded view), fetched only for the trip
    being drawn — never bundled into the day-wise list. Same RBAC as
    TripReportDataView; an unknown/not-your-fleet vehicle is 404, not 403,
    so a scoped-out uuid can't be distinguished from a made-up one.

    Query params (all required except ``end``): ``vehicle`` (uuid), ``start``
    (ISO datetime). ``end`` (ISO datetime) — omitted for an ACTIVE trip,
    meaning "up to now".
    """

    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "tracking_device"
    action = "retrieve"
    throttle_classes = [FleetReadRateThrottle]

    def get(self, request):
        vehicle_uuid = request.GET.get("vehicle", "").strip()
        start_at = parse_datetime(request.GET.get("start", "") or "")
        end_param = request.GET.get("end", "").strip()
        end_at = parse_datetime(end_param) if end_param else None
        if not vehicle_uuid or start_at is None or (end_param and end_at is None):
            return Response({"detail": "vehicle and a valid start (and, if given, end) are required."}, status=400)

        # ``max_points`` (optional): thumbnails use the default 300; the trip
        # popup asks for full detail (capped at MAX_DETAIL_ROUTE_POINTS).
        max_points = _int_param(request, "max_points", trip_report.MAX_ROUTE_POINTS)
        route = trip_report.trip_route(
            user=request.user, vehicle_uuid=vehicle_uuid, start_at=start_at, end_at=end_at, max_points=max_points
        )
        if route is None:
            return Response({"detail": "Not found."}, status=404)
        return Response(TripRouteSerializer(route).data)


# ---------------------------------------------------------------------------
# Trip Report downloads (PDF / Excel) + the MAP popup's per-trip analysis.
# Same RBAC as the Trip Report data itself (tracking_device/view): a download
# contains exactly what the caller can already see on the page, scoped by
# apps.core.scoping through trip_report.scoped_tracked_vehicles. Every
# download is written to the audit log.
# ---------------------------------------------------------------------------

EXPORT_TYPES = {
    # ``type`` rather than ``format``: DRF reserves ?format= for renderer selection.
    "pdf": ("application/pdf", "pdf"),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
}


class TripReportExportRateThrottle(UserRateThrottle):
    """Reports are heavier than a page poll — own, smaller budget."""

    scope = "trip_report_export"


def _report_error(message, status):
    return Response({"detail": message}, status=status)


def _file_response(content, file_type, filename):
    content_type, extension = EXPORT_TYPES[file_type]
    response = HttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}.{extension}"'
    response["Cache-Control"] = "no-store"
    return response


def _parse_trip_params(request):
    vehicle_uuid = request.GET.get("vehicle", "").strip()
    start_at = parse_datetime(request.GET.get("start", "") or "")
    if not vehicle_uuid or start_at is None:
        return None, None
    return vehicle_uuid, start_at


class TripReportExportView(APIView):
    """GET /api/v1/tracking/trip-report/export/?type=pdf|xlsx — the fleet
    Trip Report for the selected period, with the same ``range``/``from``/
    ``to``/``vehicle``/``q`` params as TripReportDataView (so the download
    covers exactly what the page shows). Invalid input is a 400 with a
    human-readable ``detail``, never a silently different period."""

    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "tracking_device"
    action = "list"
    throttle_classes = [TripReportExportRateThrottle]

    def get(self, request):
        file_type = request.GET.get("type", "").strip().lower()
        if file_type not in EXPORT_TYPES:
            return _report_error("Choose a download type: PDF or Excel.", 400)
        try:
            report = trip_analytics.build_period_report(
                user=request.user,
                range_key=request.GET.get("range", "today"),
                from_str=request.GET.get("from", ""),
                to_str=request.GET.get("to", ""),
                vehicle_uuid=request.GET.get("vehicle", "").strip(),
                search=request.GET.get("q", ""),
            )
            render = trip_exports.period_report_pdf if file_type == "pdf" else trip_exports.period_report_xlsx
            content = render(report)
        except trip_analytics.ReportError as error:
            return _report_error(error.message, error.status)
        except Exception:
            logger.exception("Trip report export failed")
            return _report_error("The report could not be generated. Please try again, or choose a shorter range.", 500)

        log_action(
            action=AuditLog.Action.EXPORT, module="tracking_device", entity="TripReport",
            entity_id=f"{report.start_date}..{report.end_date}",
            new_value={"type": file_type, "trips": len(report.trips), "gps_records": report.gps_total},
            user=request.user, request=request,
        )
        filename = f"trip-report_{report.start_date:%Y%m%d}"
        if report.end_date != report.start_date:
            filename += f"-{report.end_date:%Y%m%d}"
        return _file_response(content, file_type, filename)


class TripAnalysisView(APIView):
    """GET /api/v1/tracking/trip-report/trip/?vehicle=<uuid>&start=<iso> —
    the MAP popup's Trip Details + Trip Analysis for ONE trip, computed
    server-side from its full-resolution GPS history (only the summary
    crosses the wire, never the readings themselves)."""

    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "tracking_device"
    action = "retrieve"
    throttle_classes = [FleetReadRateThrottle]

    def get(self, request):
        vehicle_uuid, start_at = _parse_trip_params(request)
        if vehicle_uuid is None:
            return _report_error("vehicle and a valid start are required.", 400)
        try:
            report = trip_analytics.build_trip_report(
                user=request.user, vehicle_uuid=vehicle_uuid, start_at=start_at, keep_points=False
            )
        except trip_analytics.ReportError as error:
            return _report_error(error.message, error.status)
        return Response(trip_analytics.trip_analysis_payload(report))


class TripExportView(APIView):
    """GET /api/v1/tracking/trip-report/trip/export/?vehicle=&start=&type=pdf|xlsx
    — the individual trip report (that trip only). The trip is re-detected
    from history rather than trusting client-sent end time/distance."""

    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "tracking_device"
    action = "retrieve"
    throttle_classes = [TripReportExportRateThrottle]

    def get(self, request):
        file_type = request.GET.get("type", "").strip().lower()
        if file_type not in EXPORT_TYPES:
            return _report_error("Choose a download type: PDF or Excel.", 400)
        vehicle_uuid, start_at = _parse_trip_params(request)
        if vehicle_uuid is None:
            return _report_error("vehicle and a valid start are required.", 400)
        try:
            report = trip_analytics.build_trip_report(user=request.user, vehicle_uuid=vehicle_uuid, start_at=start_at)
            render = trip_exports.trip_report_pdf if file_type == "pdf" else trip_exports.trip_report_xlsx
            content = render(report)
        except trip_analytics.ReportError as error:
            return _report_error(error.message, error.status)
        except Exception:
            logger.exception("Trip export failed")
            return _report_error("The trip report could not be generated. Please try again.", 500)

        log_action(
            action=AuditLog.Action.EXPORT, module="tracking_device", entity="Trip",
            entity_id=report.record.trip_id[:64],
            new_value={"type": file_type, "gps_records": len(report.points)},
            user=request.user, request=request,
        )
        return _file_response(content, file_type, f"trip_{report.record.trip_id}")


# ---------------------------------------------------------------------------
# Odometer Report (apps.tracking.odometer_report): daily Device vs GPS
# odometer per vehicle. Same RBAC, scoping, query params and date-range
# handling as the Trip Report endpoints above.
# ---------------------------------------------------------------------------


class OdometerReportDataView(APIView):
    """GET /api/v1/tracking/odometer-report/ — one row per vehicle per day with
    the Device Odometer and the GPS Odometer side by side, computed server-side
    (only aggregated rows are returned, never GPS points).

    Query params: ``range`` (today/yesterday/last3/last5/last7/custom, default
    today), ``from``/``to`` (custom, YYYY-MM-DD), ``vehicle`` (uuid), ``q``.
    """

    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "tracking_device"
    action = "list"
    throttle_classes = [FleetReadRateThrottle]

    def get(self, request):
        now = timezone.now()
        tz = display_timezone()
        range_key = request.GET.get("range", "today").strip().lower()
        start_date, end_date = trip_report.resolve_date_range(
            range_key, request.GET.get("from"), request.GET.get("to"), now=now, tz=tz
        )
        report = odometer_report.build_odometer_report(
            user=request.user, start_date=start_date, end_date=end_date,
            vehicle_uuid=request.GET.get("vehicle", "").strip(), search=request.GET.get("q", ""), now=now,
        )
        return Response({
            "range": {"key": range_key, "start": start_date.isoformat(), "end": end_date.isoformat()},
            "summary": report.summary,
            "count": len(report.rows),
            "results": odometer_report.serialize_rows(report.rows),
        })


class OdometerReportExportView(APIView):
    """GET /api/v1/tracking/odometer-report/export/?type=pdf|xlsx — the same
    rows as the page for the same filters; invalid ranges are a readable 400
    (never a silently different period), like TripReportExportView."""

    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "tracking_device"
    action = "list"
    throttle_classes = [TripReportExportRateThrottle]

    def get(self, request):
        file_type = request.GET.get("type", "").strip().lower()
        if file_type not in EXPORT_TYPES:
            return _report_error("Choose a download type: PDF or Excel.", 400)
        now = timezone.now()
        tz = display_timezone()
        try:
            range_key, start_date, end_date = trip_analytics.validate_export_range(
                request.GET.get("range", "today"), request.GET.get("from", ""), request.GET.get("to", ""), now=now, tz=tz
            )
            vehicle_uuid = request.GET.get("vehicle", "").strip()
            search = request.GET.get("q", "")
            report = odometer_report.build_odometer_report(
                user=request.user, start_date=start_date, end_date=end_date,
                vehicle_uuid=vehicle_uuid, search=search, now=now,
            )
            if not report.vehicles:
                raise trip_analytics.ReportError("No GPS-tracked vehicles match the current filters.", status=404)
            if not report.summary["days_with_data"]:
                raise trip_analytics.ReportError(
                    "No odometer or GPS data was recorded for the selected vehicles and period.", status=404
                )
            render = odometer_exports.odometer_report_pdf if file_type == "pdf" else odometer_exports.odometer_report_xlsx
            content = render(report, range_key=range_key, vehicle_uuid=vehicle_uuid, search=search, now=now)
        except trip_analytics.ReportError as error:
            return _report_error(error.message, error.status)
        except Exception:
            logger.exception("Odometer report export failed")
            return _report_error("The report could not be generated. Please try again, or choose a shorter range.", 500)

        log_action(
            action=AuditLog.Action.EXPORT, module="tracking_device", entity="OdometerReport",
            entity_id=f"{start_date}..{end_date}",
            new_value={"type": file_type, "rows": len(report.rows), "vehicles": len(report.vehicles)},
            user=request.user, request=request,
        )
        filename = f"odometer-report_{start_date:%Y%m%d}"
        if end_date != start_date:
            filename += f"-{end_date:%Y%m%d}"
        return _file_response(content, file_type, filename)


# ---------------------------------------------------------------------------
# Location Data Report (apps.tracking.location_report): every GPS record of a
# vehicle (or all of the caller's vehicles) for a period, paged and sorted
# server-side, with analysis and per-record data-quality flags. Same RBAC and
# client scoping as the other reports.
# ---------------------------------------------------------------------------


def _location_selection(request):
    return location_report.resolve_selection(
        user=request.user, vehicle=request.GET.get("vehicle", ""), range_key=request.GET.get("range", "today"),
        from_str=request.GET.get("from", ""), to_str=request.GET.get("to", ""),
    )


def _int_param(request, name, default):
    try:
        return int(request.GET.get(name, default))
    except (TypeError, ValueError):
        return default


class LocationReportDataView(APIView):
    """GET /api/v1/tracking/location-report/?vehicle=<uuid|all>&range=…[&from&to]
    &page=&page_size=&sort=&dir=asc|desc&quality=|valid|issues[&analysis=1]

    ``analysis=1`` (sent when filters are applied, not on every page turn) adds
    the period analysis + data-quality summary; records are always one page."""

    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "tracking_device"
    action = "list"
    throttle_classes = [FleetReadRateThrottle]

    def get(self, request):
        quality_filter = request.GET.get("quality", "")
        if quality_filter not in location_report.QUALITY_FILTERS:
            quality_filter = ""
        try:
            selection = _location_selection(request)
            with location_report.consistent_snapshot():
                body = self._build(request, selection, quality_filter)
        except trip_analytics.ReportError as error:
            return _report_error(error.message, error.status)
        return Response(body)

    @staticmethod
    def _build(request, selection, quality_filter):
        """The page of records, its count and (if asked) the analysis — called
        inside one consistent_snapshot() so they all describe the same data."""
        page = location_report.records_page(
            selection, page=_int_param(request, "page", 1),
            page_size=_int_param(request, "page_size", location_report.DEFAULT_PAGE_SIZE),
            sort=request.GET.get("sort", "time"), direction=request.GET.get("dir", "asc"), quality=quality_filter,
        )
        body = {
            "selection": {
                "vehicle": "all" if selection.all_vehicles else str(selection.vehicles[0].uuid),
                "vehicle_label": "All vehicles" if selection.all_vehicles else selection.vehicles[0].registration_number,
                "range": {"key": selection.range_key, "start": selection.start_date.isoformat(),
                          "end": selection.end_date.isoformat()},
                "period_start": selection.period_start.isoformat(),
                "period_end": selection.period_end.isoformat(),
            },
            "records": {**{k: page[k] for k in ("total", "page", "pages", "page_size")},
                        "rows": [location_report.serialize_record(r) for r in page["rows"]]},
        }
        if request.GET.get("analysis") == "1":
            quality = location_report.quality_summary(selection)
            analysis = location_report.analyse(selection, quality)
            body["quality"] = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in quality.items()
                               if k not in location_report.OPTIONAL_COLUMNS}
            body["columns"] = [c for c in location_report.OPTIONAL_COLUMNS if quality[c]]
            body["analysis"] = (
                {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in analysis.items()}
                if analysis else None
            )
        return body


class LocationReportExportView(APIView):
    """GET /api/v1/tracking/location-report/export/?type=pdf|xlsx + the same
    filters — ALL matching records (not just the page on screen)."""

    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "tracking_device"
    action = "list"
    throttle_classes = [TripReportExportRateThrottle]

    def get(self, request):
        file_type = request.GET.get("type", "").strip().lower()
        if file_type not in EXPORT_TYPES:
            return _report_error("Choose a download type: PDF or Excel.", 400)
        quality_filter = request.GET.get("quality", "")
        if quality_filter not in location_report.QUALITY_FILTERS:
            quality_filter = ""
        try:
            selection = _location_selection(request)
            with location_report.consistent_snapshot():  # summary and rows describe the same data
                quality = location_report.quality_summary(selection)
                if not quality["records"]:
                    raise trip_analytics.ReportError(
                        "No GPS records were found for the selected vehicle and period.", status=404)
                analysis = location_report.analyse(selection, quality)
                render = location_exports.location_report_pdf if file_type == "pdf" else location_exports.location_report_xlsx
                content = render(selection, quality, analysis, quality_filter=quality_filter)
        except trip_analytics.ReportError as error:
            return _report_error(error.message, error.status)
        except Exception:
            logger.exception("Location data export failed")
            return _report_error("The report could not be generated. Please try again, or choose a shorter range.", 500)

        log_action(
            action=AuditLog.Action.EXPORT, module="tracking_device", entity="LocationDataReport",
            entity_id=f"{selection.start_date}..{selection.end_date}",
            new_value={"type": file_type, "records": quality["records"],
                       "vehicle": "all" if selection.all_vehicles else selection.vehicles[0].registration_number},
            user=request.user, request=request,
        )
        name = "all-vehicles" if selection.all_vehicles else selection.vehicles[0].registration_number
        filename = f"location-data_{name}_{selection.start_date:%Y%m%d}"
        if selection.end_date != selection.start_date:
            filename += f"-{selection.end_date:%Y%m%d}"
        return _file_response(content, file_type, filename)
