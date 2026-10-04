"""Human-facing, RBAC-authenticated read API — mounted at
``/api/v1/tracking/`` (config/urls.py). Nested-under-vehicle URL shape, so
plain paths rather than a DefaultRouter."""

from django.urls import path

from apps.tracking import api_views

urlpatterns = [
    path("fleet/current/", api_views.FleetCurrentTelemetryView.as_view(), name="fleet-telemetry-current"),
    path("trip-report/", api_views.TripReportDataView.as_view(), name="trip-report-data"),
    path("trip-report/route/", api_views.TripRouteView.as_view(), name="trip-report-route"),
    path("trip-report/export/", api_views.TripReportExportView.as_view(), name="trip-report-export"),
    path("trip-report/trip/", api_views.TripAnalysisView.as_view(), name="trip-report-trip"),
    path("trip-report/trip/export/", api_views.TripExportView.as_view(), name="trip-report-trip-export"),
    path("odometer-report/", api_views.OdometerReportDataView.as_view(), name="odometer-report-data"),
    path("odometer-report/export/", api_views.OdometerReportExportView.as_view(), name="odometer-report-export"),
    path("location-report/", api_views.LocationReportDataView.as_view(), name="location-report-data"),
    path("location-report/export/", api_views.LocationReportExportView.as_view(), name="location-report-export"),
    path(
        "vehicles/<uuid:vehicle_uuid>/telemetry/current/",
        api_views.VehicleCurrentTelemetryView.as_view(),
        name="vehicle-telemetry-current",
    ),
    path(
        "vehicles/<uuid:vehicle_uuid>/telemetry/history/",
        api_views.VehicleTelemetryHistoryView.as_view(),
        name="vehicle-telemetry-history",
    ),
]
