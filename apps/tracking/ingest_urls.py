"""Device-authenticated ingestion endpoint — mounted at
``/api/telematics/`` (config/urls.py), deliberately outside ``/api/v1/``
since it uses an entirely different auth stack (DeviceKeyAuthentication),
not the session/RBAC surface apps.accounts established there."""

from django.urls import path

from apps.tracking import api_views

urlpatterns = [
    path("ingest/", api_views.TelemetryIngestView.as_view(), name="telemetry-ingest"),
]
