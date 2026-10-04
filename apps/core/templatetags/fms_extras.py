from django import template

from apps.core.permissions import user_has_permission

register = template.Library()


# Maps a raw status/action value to a CSS "tone" consumed by the .status-pill
# component (components.css: tone-success/warning/danger/info/purple/neutral).
_STATUS_BADGE_MAP = {
    "ACTIVE": "success",
    "VALID": "success",
    "VERIFIED": "success",
    "APPROVED": "success",
    "COMPLETED": "success",
    "AVAILABLE": "success",
    "INACTIVE": "neutral",
    "CLOSED": "neutral",
    "PROSPECT": "info",
    "DRAFT": "neutral",
    "PENDING_VERIFICATION": "warning",
    "EXPIRING_SOON": "warning",
    "ON_LEAVE": "warning",
    "INVITED": "purple",
    "ASSIGNED": "info",
    "ON_TRIP": "info",
    "IN_PROGRESS": "info",
    "SUSPENDED": "danger",
    "LOCKED": "danger",
    "EXPIRED": "danger",
    "REJECTED": "danger",
    "TERMINATED": "danger",
    "CANCELLED": "danger",
    "FAILED": "danger",
    "ACCIDENT": "danger",
    "UNDER_MAINTENANCE": "warning",
    "MAINTENANCE": "warning",
    "UNAVAILABLE": "neutral",
    # Trip statuses
    "SCHEDULED": "purple",
    "DISPATCHED": "info",
    "DELAYED": "warning",
    # Telemetry connection status (apps.tracking.services.connection_status_for)
    "ONLINE": "success",
    "STALE": "warning",
    "OFFLINE": "neutral",
    # Trip priority
    "LOW": "neutral",
    "NORMAL": "info",
    "HIGH": "warning",
    "URGENT": "danger",
    # Maintenance priority (MEDIUM/CRITICAL only — LOW/HIGH shared with Trip above)
    "MEDIUM": "info",
    "CRITICAL": "danger",
    # Maintenance urgency (computed, see Maintenance.urgency)
    "DUE_SOON": "warning",
    "DUE_TODAY": "warning",
    "OVERDUE": "danger",
    # Document expiry (computed, see Document.expiry_status)
    "NO_EXPIRY": "neutral",
    "ARCHIVED": "neutral",
    # Audit log actions
    "CREATE": "success",
    "UPDATE": "info",
    "ARCHIVE": "warning",
    "RESTORE": "success",
    "LOGIN": "success",
    "LOGOUT": "neutral",
    "ASSIGN": "info",
    "UNASSIGN": "neutral",
    "APPROVE": "success",
    "REJECT": "danger",
    "EXPORT": "purple",
    "DISPATCH": "info",
    "START": "info",
    "DELAY": "warning",
    "RESUME": "info",
    "COMPLETE": "success",
    "CANCEL": "danger",
}


@register.simple_tag
def has_module_perm(user, module, action):
    return user_has_permission(user, module, action)


@register.filter
def status_badge(status_value):
    if not status_value:
        return "neutral"
    return _STATUS_BADGE_MAP.get(str(status_value).upper(), "neutral")


# Maps AuditLog.Action values to a real Bootstrap Icons class — there is no
# bi-create/bi-login/etc., so `{{ log.action|lower }}` used directly against
# the "bi-" prefix would silently render no icon at all.
_ACTION_ICON_MAP = {
    "CREATE": "plus-circle",
    "UPDATE": "pencil",
    "ARCHIVE": "archive",
    "RESTORE": "arrow-counterclockwise",
    "LOGIN": "box-arrow-in-right",
    "LOGOUT": "box-arrow-right",
    "ASSIGN": "link-45deg",
    "UNASSIGN": "x-circle",
    "APPROVE": "check-circle",
    "REJECT": "slash-circle",
    "EXPORT": "download",
    "DISPATCH": "send",
    "START": "play-circle",
    "DELAY": "exclamation-triangle",
    "RESUME": "arrow-clockwise",
    "COMPLETE": "check-circle",
    "CANCEL": "x-circle",
    "DELETE": "trash",
    "DOWNLOAD": "download",
}


@register.filter
def action_icon(action_value):
    return _ACTION_ICON_MAP.get(str(action_value).upper(), "activity")


# Driver.license_expiry_status returns a raw code (VALID/EXPIRING_SOON/EXPIRED)
# rather than a model field, so it has no get_FOO_display(). Centralizing the
# label mapping here means driver_list.html and driver_detail.html don't each
# duplicate the same if/elif chain.
_LICENSE_STATUS_LABELS = {
    "VALID": "Valid",
    "EXPIRING_SOON": "Expiring Soon",
    "EXPIRED": "Expired",
}


@register.filter
def license_status_label(value):
    return _LICENSE_STATUS_LABELS.get(value, "")


@register.filter
def initials(user):
    if not user or not getattr(user, "is_authenticated", False):
        return "?"
    first = (user.first_name or "")[:1]
    last = (user.last_name or "")[:1]
    combined = (first + last).upper()
    return combined or (user.username or "?")[:2].upper()


@register.simple_tag(takes_context=True)
def querystring_replace(context, **kwargs):
    """Rebuilds the current GET querystring with the given keys replaced — used
    for pagination/sort links that must preserve active search/filter params."""
    request = context["request"]
    params = request.GET.copy()
    for key, value in kwargs.items():
        if value in (None, ""):
            params.pop(key, None)
        else:
            params[key] = value
    return params.urlencode()


@register.filter
def duration_display(value):
    """Formats a timedelta as "4h 30m" — used for trip.estimated_duration
    instead of Python's default "4:30:00" str()."""
    if value is None:
        return "—"
    total_minutes = int(value.total_seconds() // 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


@register.filter
def make_range(value):
    """``{% for _ in 5|make_range %}`` — used by the skeleton loader component."""
    try:
        return range(int(value))
    except (TypeError, ValueError):
        return range(0)


@register.filter
def dict_get(mapping, key):
    """``{{ row|dict_get:column }}`` — looks up a dict value by a variable
    key, which Django's dotted attribute lookup (``row.column``) can't do
    since ``column`` is itself a template variable, not a literal name.
    Used by the Reports module to render each report's dynamic column set
    without a dedicated template per report."""
    if mapping is None:
        return ""
    return mapping.get(key, "")
