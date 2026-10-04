"""
Django settings for the FMS Admin Portal.

Configuration is driven entirely by environment variables (see .env.example)
so the same codebase runs unchanged in development, staging, and production.
"""

from pathlib import Path

import environ
from django.contrib.messages import constants as message_constants

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
)
environ.Env.read_env(BASE_DIR / ".env")

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
SECRET_KEY = env("SECRET_KEY", default="django-insecure-dev-only-change-me")
DEBUG = env.bool("DEBUG", default=False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1", "125.99.240.87"])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    # Third-party
    "rest_framework",
    "django_filters",
    "drf_spectacular",
    "django_celery_beat",
    # FMS apps — Phase 1 (fully implemented)
    "apps.core",
    "apps.accounts",
    "apps.audit",
    # FMS apps — scaffolded now, implemented in later phases
    "apps.clients",
    "apps.locations",
    "apps.vendors",
    "apps.contracts",
    "apps.vehicles",
    "apps.drivers",
    "apps.documents",
    "apps.tracking",
    "apps.maintenance",
    "apps.routes",
    "apps.trips",
    "apps.geofences",
    "apps.alerts",
    "apps.notifications",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.audit.middleware.CurrentRequestMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.sidebar_nav",
                "apps.core.context_processors.site_meta",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default="postgres://fms_user:fms_password@localhost:5432/fms_db",
    )
}

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

MESSAGE_TAGS = {
    message_constants.DEBUG: "secondary",
    message_constants.INFO: "info",
    message_constants.SUCCESS: "success",
    message_constants.WARNING: "warning",
    message_constants.ERROR: "danger",
}

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

# ---------------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE", default="UTC")
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static / media
# ---------------------------------------------------------------------------
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# The hashed/manifest storage requires `collectstatic` to have run first (it looks
# up a staticfiles.json manifest). That's fine for production, where collectstatic
# is part of the deploy step (see docker-compose.yml), but would break a fresh
# `runserver` in development before anyone has run it — so plain storage in DEBUG.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

MAX_UPLOAD_SIZE_MB = env.int("MAX_UPLOAD_SIZE_MB", default=5)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", default="localhost")
EMAIL_PORT = env.int("EMAIL_PORT", default=25)
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=False)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="FMS Admin <no-reply@example.com>")

# ---------------------------------------------------------------------------
# Celery
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="redis://localhost:6379/1")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.StandardResultsSetPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "user": "1000/hour",
        "fleet_read": "4000/hour",  # Live Tracking polls every 5-300 s; see FleetReadRateThrottle
        "trip_report_export": "120/hour",  # PDF/Excel downloads; see TripReportExportRateThrottle
        "device_ingest": "120/min",
    },
}

SPECTACULAR_SETTINGS = {
    "TITLE": "FMS Admin Portal API",
    "DESCRIPTION": "Internal REST API for the Fleet Management System admin portal.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    # drf-spectacular defaults to AllowAny, which would publish the whole
    # API surface (routes, field names) to anonymous visitors.
    "SERVE_PERMISSIONS": ["rest_framework.permissions.IsAuthenticated"],
}

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False
SESSION_COOKIE_AGE = env.int("SESSION_COOKIE_AGE", default=8 * 60 * 60)
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True

if not DEBUG:
    # Off by default: docker-compose ships gunicorn with no TLS-terminating proxy in
    # front of it out of the box, so a blanket SSL redirect would just 301-loop.
    # Set SECURE_SSL_REDIRECT=True once a real TLS-terminating proxy sits in front.
    SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=False)
    SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", default=False)
    CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", default=False)
    SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=0)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
    SECURE_HSTS_PRELOAD = SECURE_HSTS_SECONDS > 0

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {name} - {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": env("DJANGO_LOG_LEVEL", default="INFO"),
    },
    "loggers": {
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
    },
}

# ---------------------------------------------------------------------------
# FMS-specific settings
# ---------------------------------------------------------------------------
FMS_APP_NAME = "FMS Admin Portal"
FMS_COMPANY_NAME = env("FMS_COMPANY_NAME", default="Zentora Info Global Solutions")

# ---------------------------------------------------------------------------
# Telematics / GPS ingestion (Phase 3.3)
# ---------------------------------------------------------------------------
# Vehicle connection status (apps.tracking.services.connection_status_for) is
# computed live from "now - last telemetry timestamp" against these
# thresholds — never stored, so it can never itself go stale. Consumed by
# multiple unrelated layers (read API, vehicle detail, device detail), so
# this lives here rather than as a single model's constant.
# Zentora comms program (D:\comms\src) stores parsed device data in its own
# PostgreSQL "App DB" (current_table / history_table). apps.tracking.comms_sync
# reads it (read-only) and feeds the FMS tracking tables. Leave empty to
# disable the bridge. Example: postgres://user:password@localhost:5432/APPDB
COMMS_APP_DATABASE_URL = env("COMMS_APP_DATABASE_URL", default="")
# Optional: the comms "Raw DB" (teltonika_raw_table). The App DB keeps only the
# panic flag its stored procedure derives (analog1 / 1000 >= 10 V); with this set,
# the bridge also reads that analog1 voltage for panic readings so Panic alerts
# show it. Read-only. Example: postgres://user:password@localhost:5432/COMMSDB
COMMS_RAW_DATABASE_URL = env("COMMS_RAW_DATABASE_URL", default="")
# Keep the FMS tables fed from the comms App DB by a background thread inside the web process
# (runserver / WSGI / ASGI) — no separate `manage.py sync_comms_data --loop` needed. Off by default.
COMMS_SYNC_AUTOSTART = env.bool("COMMS_SYNC_AUTOSTART", default=False)
COMMS_SYNC_INTERVAL_SECONDS = env.int("COMMS_SYNC_INTERVAL_SECONDS", default=10)

# Alert events (apps.alerts.events): only events newer than this notify users
# (bell + popup + sound) — a first import of old history must not page everyone.
ALERT_NOTIFY_MAX_AGE_MINUTES = env.int("ALERT_NOTIFY_MAX_AGE_MINUTES", default=1440)
# Idle alerts (apps.alerts.idle). The idle threshold itself is per vehicle (Vehicle.idle_alert_minutes)
# and "stationary" uses each vehicle's movement speed/distance. These two guard the data:
# a gap between readings longer than this ends an idle run (missing data is never counted as idling;
# devices here report every <= 30 s with the ignition on) ...
IDLE_ALERT_MAX_GAP_SECONDS = env.int("IDLE_ALERT_MAX_GAP_SECONDS", default=180)
# ... and an idle run needs at least this many readings before it can raise an alert.
IDLE_ALERT_MIN_READINGS = env.int("IDLE_ALERT_MIN_READINGS", default=3)
# Over Speeding alerts (apps.alerts.overspeed): the device sends an eventioval-255 record at the start
# and at the end of an overspeed; a start pairs with the next 255 only within this many minutes.
OVERSPEED_MAX_EPISODE_MINUTES = env.int("OVERSPEED_MAX_EPISODE_MINUTES", default=30)
# Geofences (apps.geofences.services): a vehicle counts as having LEFT a geofence only once it is this far
# outside the boundary (GPS jitter on the edge can't flap Entry/Exit) ...
GEOFENCE_EXIT_TOLERANCE_METERS = env.int("GEOFENCE_EXIT_TOLERANCE_METERS", default=20)
# ... and a crossing must hold for this many consecutive readings (one GPS jump is not an entry).
GEOFENCE_CONFIRM_READINGS = env.int("GEOFENCE_CONFIRM_READINGS", default=2)

TELEMATICS_ONLINE_THRESHOLD_MINUTES = env.int("TELEMATICS_ONLINE_THRESHOLD_MINUTES", default=5)
TELEMATICS_OFFLINE_THRESHOLD_MINUTES = env.int("TELEMATICS_OFFLINE_THRESHOLD_MINUTES", default=30)
# Live Fleet Map (Phase 3.4) movement-state helper — the single place this
# threshold is applied; no frontend file may hardcode a speed cutoff.
TELEMATICS_MOVEMENT_SPEED_THRESHOLD_KMH = env.int("TELEMATICS_MOVEMENT_SPEED_THRESHOLD_KMH", default=5)
# Reserved for a future cleanup management command; not enforced this phase.
TELEMATICS_RAW_EVENT_RETENTION_DAYS = env.int("TELEMATICS_RAW_EVENT_RETENTION_DAYS", default=30)

# Trip Report (apps.tracking.trip_report) — ignition-derived vehicle trips computed
# on the fly from TelemetryEvent history. How long ignition must stay OFF to close
# a trip is a per-vehicle setting (Vehicle.trip_closure_minutes, default 5), not
# a global one — see apps.vehicles.models.DEFAULT_TRIP_CLOSURE_MINUTES.

# Trip Report downloads (apps.tracking.trip_exports): the single-trip PDF/Excel
# draws the real GPS route over map tiles fetched server-side (cached 7 days).
# Same OSM demo server the browser maps use (static/js/fms_map.js) — point it
# at a paid/self-hosted tile server for production volume, or set it empty to
# draw the route on a plain grid without any outbound request.
TRIP_REPORT_MAP_TILE_URL = env("TRIP_REPORT_MAP_TILE_URL", default="https://tile.openstreetmap.org/{z}/{x}/{y}.png")
TRIP_REPORT_MAP_TILE_TIMEOUT = env.int("TRIP_REPORT_MAP_TILE_TIMEOUT", default=5)
TRIP_REPORT_MAP_USER_AGENT = env("TRIP_REPORT_MAP_USER_AGENT", default="ZentoraFMS-TripReport/1.0")

# --- Telematics Communication Service (Phase 3.5) --------------------------
# Standalone asyncio TCP server, run via `manage.py run_telematics_server` as
# a separate OS process from the web app — see apps/tracking/telematics_service.
TELEMATICS_TCP_HOST = env("TELEMATICS_TCP_HOST", default="0.0.0.0")
TELEMATICS_TCP_PORT = env.int("TELEMATICS_TCP_PORT", default=9000)
# NDJSON framer guard — a line longer than this is a protocol violation
# (malformed/oversized); the connection is dropped cleanly, never crashes
# the accept loop.
TELEMATICS_MAX_FRAME_BYTES = env.int("TELEMATICS_MAX_FRAME_BYTES", default=65536)
# A connection that sends no `identify` message within this window is dropped.
TELEMATICS_IDENTIFY_TIMEOUT_SECONDS = env.int("TELEMATICS_IDENTIFY_TIMEOUT_SECONDS", default=10)
# A connection silent (no message of any type) for this long is presumed
# dead and dropped — distinct from the command ACK timeout below.
TELEMATICS_CONNECTION_IDLE_TIMEOUT_SECONDS = env.int("TELEMATICS_CONNECTION_IDLE_TIMEOUT_SECONDS", default=180)
# One periodic tick does both: (1) claim+dispatch PENDING commands for all
# currently-connected devices in one query, (2) sweep SENT commands past ACK
# timeout and PENDING/QUEUED commands past expires_at.
TELEMATICS_COMMAND_POLL_INTERVAL_SECONDS = env.int("TELEMATICS_COMMAND_POLL_INTERVAL_SECONDS", default=5)
TELEMATICS_COMMAND_ACK_TIMEOUT_SECONDS = env.int("TELEMATICS_COMMAND_ACK_TIMEOUT_SECONDS", default=60)
TELEMATICS_COMMAND_DEFAULT_MAX_RETRIES = env.int("TELEMATICS_COMMAND_DEFAULT_MAX_RETRIES", default=3)

# --- Teltonika Codec 8 protocol adapter (Phase 3.6) -------------------------
# Binary framing from byte one, so it gets its own TCP listener rather than
# sharing the generic NDJSON port (see apps/tracking/telematics_service/protocol/teltonika.py).
TELEMATICS_TELTONIKA_TCP_PORT = env.int("TELEMATICS_TELTONIKA_TCP_PORT", default=9001)
# Max declared AVL data-field length accepted before a packet is treated as
# oversized/malformed and the connection is dropped.
TELEMATICS_TELTONIKA_MAX_FRAME_BYTES = env.int("TELEMATICS_TELTONIKA_MAX_FRAME_BYTES", default=8192)
