from apps.notifications.models import Notification


def notify(recipient, title, body="", level=Notification.Level.INFO, link_url="", created_by=None, alert=None):
    """The one place a Notification row gets created — every trigger point
    elsewhere in the app (Trip creation, broadcasts, ...) calls this instead
    of ``Notification.objects.create`` directly, so the shape of a
    notification never has to be re-derived at each call site."""
    return Notification.objects.create(
        recipient=recipient, title=title, body=body, level=level, link_url=link_url, created_by=created_by,
        alert=alert,
    )


def notify_many(recipients, title, body="", level=Notification.Level.INFO, link_url="", created_by=None, alert=None):
    notifications = [
        Notification(recipient=user, title=title, body=body, level=level, link_url=link_url, created_by=created_by,
                     alert=alert)
        for user in recipients
    ]
    return Notification.objects.bulk_create(notifications)


def unread_count(user):
    return Notification.objects.filter(recipient=user, is_read=False, is_archived=False).count()


ANNOUNCE_LIMIT = 10


def _num(value):
    return f"{value:.0f}" if value is not None else None


def alert_toast_detail(alert):
    """One short line for the compact toast, from the alert's own data (the full
    message stays in the bell / Alert Report). Falls back to ``alert.message``."""
    category = alert.category
    place = str(alert.content_object) if alert.object_id and alert.content_object is not None else ""
    if category == "PANIC":
        return "Panic button pressed"
    if category == "OVER_SPEEDING" and alert.speed is not None:
        return f"Speed: {_num(alert.speed)} km/h"
    if category == "GEOFENCE_ENTRY" and place:
        return f"Entered: {place}"
    if category == "GEOFENCE_EXIT" and place:
        return f"Exited: {place}"
    if category == "GEOFENCE_SPEEDING" and alert.speed is not None:
        limit = f" (limit {alert.speed_limit} km/h)" if alert.speed_limit else ""
        return f"Speed: {_num(alert.speed)} km/h{limit}" + (f" · {place}" if place else "")
    return (alert.message or alert.get_category_display()).rstrip(".")


def unread_alert_announcements(user):
    """The user's unread alert notifications (newest first) with what the
    emergency popup shows — vehicle, driver, time, location — read from the
    Alert row itself, so popup, bell, report and exports never disagree."""
    qs = (
        Notification.objects.filter(recipient=user, is_read=False, is_archived=False, alert__isnull=False)
        .select_related("alert", "alert__vehicle", "alert__driver")
        .prefetch_related("alert__content_object")  # the geofence, for the toast's one-line detail
        .order_by("-created_at")[:ANNOUNCE_LIMIT]
    )
    out = []
    for n in qs:
        alert = n.alert
        out.append({
            "notification_uuid": str(n.uuid),
            "alert_uuid": str(alert.uuid),
            "type": alert.category,
            "type_label": alert.get_category_display(),
            "severity": alert.severity,
            "level_label": alert.get_severity_display(),
            "message": alert.message if alert.category != "PANIC" else "",
            "detail": alert_toast_detail(alert),
            "vehicle": alert.vehicle.registration_number if alert.vehicle_id else "",
            "driver": alert.driver.get_full_name() if alert.driver_id else "",
            "occurred_at": (alert.triggered_at or alert.occurred_at).isoformat() if alert.occurred_at else None,
            "location": alert.location,
            "latitude": float(alert.latitude) if alert.latitude is not None else None,
            "longitude": float(alert.longitude) if alert.longitude is not None else None,
            "link_url": n.link_url,
            "created_at": n.created_at.isoformat(),
        })
    return out
