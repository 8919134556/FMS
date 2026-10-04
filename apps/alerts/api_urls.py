"""RBAC-authenticated Alert Report API — mounted at ``/api/v1/alerts/`` (config/urls.py)."""

from django.urls import path

from apps.alerts import api_views

urlpatterns = [
    path("report/", api_views.AlertReportDataView.as_view(), name="alert-report-data"),
    path("report/export/", api_views.AlertReportExportView.as_view(), name="alert-report-export"),
    path("<uuid:uuid>/", api_views.AlertDetailView.as_view(), name="alert-detail-api"),
]
