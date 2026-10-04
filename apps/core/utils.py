def display_timezone():
    """The org-wide display timezone (Settings > Localization > Default timezone,
    falling back to ``settings.TIME_ZONE``) as a ``ZoneInfo`` — the single place
    "what timezone do we show times in" is resolved, so comms ingestion, the Trip
    Report's day boundaries, and any future feature all agree with the Settings page."""
    from zoneinfo import ZoneInfo

    from django.conf import settings

    from apps.core.models import SystemSettings

    name = SystemSettings.load().default_timezone or settings.TIME_ZONE
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(settings.TIME_ZONE)


def get_client_ip(request) -> str | None:
    """Best-effort client IP resolution, respecting a trusted reverse proxy header."""
    if request is None:
        return None
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def get_user_agent(request) -> str:
    if request is None:
        return ""
    return request.META.get("HTTP_USER_AGENT", "")[:255]


def model_to_dict_safe(instance, fields: list[str]) -> dict:
    """Serialize selected model fields to plain JSON-safe values, for audit diffs."""
    data = {}
    for field_name in fields:
        value = getattr(instance, field_name, None)
        if value is None or isinstance(value, (str, int, float, bool)):
            data[field_name] = value
        else:
            data[field_name] = str(value)
    return data
