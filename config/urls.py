"""
Root URL configuration.

Layout:
    /                       -> redirect to dashboard (or login)
    /dashboard/             -> apps.core     (dashboard, cross-module views)
    /accounts/              -> apps.accounts (login/logout/password management)
    /admin/users/           -> apps.accounts (user administration UI)
    /admin/roles/           -> apps.accounts (role/permission administration UI)
    /admin/audit-logs/      -> apps.audit    (read-only audit trail UI)
    /fleet/vehicles/        -> apps.vehicles (vehicle CRUD + driver assignment)
    /fleet/drivers/         -> apps.drivers  (driver CRUD)
    /clients/               -> apps.clients  (client CRUD)
    /sites/                 -> apps.locations (client site CRUD)
    /trips/                 -> apps.trips    (trip CRUD + lifecycle workflow)
    /dispatch/              -> apps.trips    (Dispatch Board — orchestration over the same trip services)
    /maintenance/           -> apps.maintenance (maintenance CRUD + start/complete/cancel workflow)
    /documents/             -> apps.documents (generic document attachments + protected download)
    /tracking/              -> apps.tracking  (GPS device management UI)
    /django-admin/          -> Django's built-in admin site (superuser break-glass access)
    /legal/terms/, /legal/privacy/ -> public, unauthenticated informational pages
    /api/v1/...             -> apps.accounts DRF API
    /api/v1/tracking/...    -> apps.tracking  (RBAC'd read-only telemetry API)
    /api/v1/alerts/...      -> apps.alerts    (RBAC'd, client-scoped Alert Report API)
    /api/telematics/ingest/ -> apps.tracking  (device-key-authenticated GPS ingestion, NOT RBAC'd/versioned)
    /api/schema/, /api/docs/-> OpenAPI schema + Swagger UI
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.urls import include, path
from django.views.generic import RedirectView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.core.views import PrivacyView, TermsView

urlpatterns = [
    path("", login_required(RedirectView.as_view(pattern_name="core:dashboard", permanent=False)), name="home"),
    path("django-admin/", admin.site.urls),
    path("dashboard/", include("apps.core.urls", namespace="core")),
    path("accounts/", include("apps.accounts.urls", namespace="accounts")),
    path("admin/", include("apps.accounts.portal_urls", namespace="accounts_portal")),
    path("admin/", include("apps.audit.urls", namespace="audit")),
    path("fleet/", include("apps.vehicles.urls", namespace="vehicles")),
    path("fleet/", include("apps.drivers.urls", namespace="drivers")),
    path("clients/", include("apps.clients.urls", namespace="clients")),
    path("sites/", include("apps.locations.urls", namespace="locations")),
    path("vendors/", include("apps.vendors.urls", namespace="vendors")),
    path("contracts/", include("apps.contracts.urls", namespace="contracts")),
    path("trips/", include("apps.trips.urls", namespace="trips")),
    path("dispatch/", include("apps.trips.dispatch_urls", namespace="dispatch")),
    path("maintenance/", include("apps.maintenance.urls", namespace="maintenance")),
    path("documents/", include("apps.documents.urls", namespace="documents")),
    path("tracking/", include("apps.tracking.urls", namespace="tracking")),
    path("notifications/", include("apps.notifications.urls", namespace="notifications")),
    path("alerts/", include("apps.alerts.urls", namespace="alerts")),
    path("geofences/", include("apps.geofences.urls", namespace="geofences")),
    path("routes/", include("apps.routes.urls", namespace="routes")),
    path("legal/terms/", TermsView.as_view(), name="terms"),
    path("legal/privacy/", PrivacyView.as_view(), name="privacy"),
    path("api/v1/", include("apps.accounts.api_urls")),
    path("api/v1/tracking/", include("apps.tracking.api_urls")),
    path("api/v1/alerts/", include("apps.alerts.api_urls")),
    path("api/telematics/", include("apps.tracking.ingest_urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="api-docs"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
