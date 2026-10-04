from django.conf import settings
from django.urls import NoReverseMatch, reverse

from apps.core.permissions import user_has_any_permission, user_has_permission
from apps.core.scoping import is_client_scoped

# Every module the finished FMS admin portal will expose, in sidebar order.
# ``url_name`` is None for modules not yet implemented — they render as a
# locked entry tagged with the phase that will deliver them, instead of a
# dead link or a fake page. ``key`` is a stable slug used to persist each
# group's expand/collapse state in localStorage (see static/js/app.js).
NAV_SECTIONS = [
    {
        "key": "overview",
        "heading": "Overview",
        "items": [
            {"label": "Dashboard", "icon": "speedometer2", "url_name": "core:dashboard", "module": None, "phase": 1},
        ],
    },
    {
        "key": "tracking",
        "heading": "Tracking",
        "items": [
            {"label": "Live Tracking", "icon": "broadcast", "url_name": "tracking:live_map", "module": "tracking_device", "phase": 3},
            {"label": "Geofences", "icon": "pin-map", "url_name": "geofences:geofence_list", "module": "geofence", "phase": 5},
        ],
    },
    {
        # Every report lives here. To add a report type, append one item — it
        # follows the same RBAC rule as every other entry (shown only to roles
        # with "view" on its ``module``); no template or JS change is needed.
        "key": "reports",
        "heading": "Reports",
        "items": [
            {"label": "Trip Report", "icon": "clock-history", "url_name": "tracking:trip_report", "module": "tracking_device", "phase": 3},
            {"label": "Alert Report", "icon": "bell", "url_name": "alerts:alert_report", "module": "alert", "phase": 5},
            {"label": "Odometer Report", "icon": "speedometer", "url_name": "tracking:odometer_report", "module": "tracking_device", "phase": 3},
            {"label": "Location Data Report", "icon": "map", "url_name": "tracking:location_report", "module": "tracking_device", "phase": 3},
        ],
    },
    {
        "key": "fleet",
        "heading": "Fleet",
        "items": [
            {"label": "Vehicles", "icon": "truck-front", "url_name": "vehicles:vehicle_list", "module": "vehicle", "phase": 2},
            {"label": "Vehicle Types", "icon": "tags", "url_name": "vehicles:vehicle_type_list", "module": "vehicle_type", "phase": 2},
            {"label": "Drivers", "icon": "person-badge", "url_name": "drivers:driver_list", "module": "driver", "phase": 2},
            {"label": "Assignments", "icon": "link-45deg", "url_name": "vehicles:assignment_list", "module": "assignment", "phase": 2},
            {"label": "GPS Devices", "icon": "cpu", "url_name": "tracking:device_list", "module": "tracking_device", "phase": 3},
        ],
    },
    {
        "key": "clients",
        "heading": "Clients",
        "items": [
            {"label": "Clients", "icon": "building", "url_name": "clients:client_list", "module": "client", "phase": 2},
            {"label": "Sites", "icon": "geo-alt", "url_name": "locations:site_list", "module": "site", "phase": 2},
            {"label": "Contracts", "icon": "file-earmark-text", "url_name": "contracts:contract_list", "module": "contract", "phase": 2},
            {"label": "Vendors", "icon": "truck", "url_name": "vendors:vendor_list", "module": "vendor", "phase": 2},
            {"label": "Branches / Depots", "icon": "diagram-3", "url_name": "locations:branch_list", "module": "branch", "phase": 2},
        ],
    },
    {
        "key": "operations",
        "heading": "Operations",
        "items": [
            {"label": "Dispatch", "icon": "send", "url_name": "dispatch:board", "module": "trip", "staff_only": True, "phase": 3},
            {"label": "Routes", "icon": "signpost-split", "url_name": "routes:route_list", "module": "route", "phase": 6},
        ],
    },
    {
        "key": "maintenance",
        "heading": "Maintenance",
        "items": [
            {"label": "Maintenance", "icon": "tools", "url_name": "maintenance:maintenance_list", "module": "maintenance", "phase": 2},
            {"label": "Service Schedule", "icon": "calendar-check", "url_name": "maintenance:service_schedule", "module": "maintenance", "phase": 2},
        ],
    },
    {
        "key": "documents",
        "heading": "Documents",
        "items": [
            {"label": "All Documents", "icon": "file-earmark-text", "url_name": "documents:document_list", "module": "document", "phase": 2},
            {"label": "Vehicle Documents", "icon": "file-earmark-check", "url_name": "documents:document_list", "query": "entity_type=vehicle", "module": "document", "phase": 2},
            {"label": "Driver Documents", "icon": "file-earmark-person", "url_name": "documents:document_list", "query": "entity_type=driver", "module": "document", "phase": 2},
            {"label": "Expiring Documents", "icon": "hourglass-split", "url_name": "documents:document_list", "query": "expiry=expiring_soon", "module": "document", "phase": 2},
        ],
    },
    {
        "key": "administration",
        "heading": "Administration",
        "items": [
            {"label": "Users", "icon": "people", "url_name": "accounts_portal:user_list", "module": "user", "phase": 1},
            {"label": "Roles & Permissions", "icon": "shield-lock", "url_name": "accounts_portal:role_list", "module": "role", "phase": 1},
            {"label": "Master Data", "icon": "database", "url_name": "core:master_data", "module": None, "modules_any": ["client", "site", "branch"], "staff_only": True, "phase": 2},
            {"label": "Audit Logs", "icon": "journal-text", "url_name": "audit:audit_log_list", "module": "audit_log", "phase": 1},
            {"label": "Settings", "icon": "gear", "url_name": "core:settings", "module": "settings", "phase": 8},
        ],
    },
]


def sidebar_nav(request):
    """Builds the sidebar structure: resolves URLs and applies RBAC visibility.

    An item the viewer can't access is *omitted entirely* — not rendered
    disabled/locked — and a section left with no visible items disappears
    with it. That's presentation only; every target view still enforces
    its own permission server-side (see apps.core.permissions).

    Also returns ``active_nav_label``/``active_nav_section`` — the currently
    matched item's own label and its section heading — so the topbar
    breadcrumb (components/navbar.html) always reflects wherever the sidebar
    says you are, without every view having to pass its own breadcrumb text.
    """
    user = getattr(request, "user", None)
    sections = []
    active_label = None
    active_section = None
    for section in NAV_SECTIONS:
        items = []
        has_active = False
        for item in section["items"]:
            # Internal-only tools (dispatch board, master-data hub) aren't
            # client-scoped, so client users never get a link to them.
            if item.get("staff_only") and is_client_scoped(user):
                continue
            if item.get("modules_any"):
                can_view = user_has_any_permission(user, item["modules_any"], "view")
            elif item["module"] is None:
                can_view = True
            else:
                can_view = user_has_permission(user, item["module"], "view")
            if not can_view:
                continue

            base_url = None
            if item["url_name"]:
                try:
                    base_url = reverse(item["url_name"])
                except NoReverseMatch:
                    base_url = None
            is_built = base_url is not None
            # "current" is matched on the path alone — a query string (see
            # the "query" key some items set, e.g. Documents shortcuts)
            # would otherwise never match request.path, which never
            # includes one.
            current = bool(base_url and request.path.startswith(base_url))
            has_active = has_active or current
            if current:
                active_label = item["label"]
                active_section = section["heading"]
            url = base_url
            if base_url and item.get("query"):
                url = f"{base_url}?{item['query']}"
            items.append({**item, "url": url, "enabled": is_built, "current": current})
        if items:
            sections.append({"key": section["key"], "heading": section["heading"], "items": items, "has_active": has_active})
    return {
        "nav_sections": sections,
        "active_nav_label": active_label,
        "active_nav_section": active_section,
    }


def site_meta(request):
    from apps.core.models import SystemSettings

    # DB-configured company name (Settings page) overrides the env default,
    # so an admin's change is visible everywhere immediately, no redeploy.
    system_settings = SystemSettings.load()
    company_name = system_settings.company_name
    return {
        "fms_app_name": getattr(settings, "FMS_APP_NAME", "FMS Admin Portal"),
        "fms_company_name": company_name or getattr(settings, "FMS_COMPANY_NAME", ""),
        # Lets global UI (the alert popup) show times in the org's timezone.
        "fms_display_timezone": system_settings.default_timezone or "UTC",
    }
