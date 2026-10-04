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


def unread_alert_announcements(user):
    """The user's unread alert notifications (newest first) with what the
    emergency popup shows — vehicle, driver, time, location — read from the
    Alert row itself, so popup, bell, report and exports never disagree."""
    qs = (
        Notification.objects.filter(recipient=user, is_read=False, is_archived=False, alert__isnull=False)
        .select_related("alert", "alert__vehicle", "alert__driver")
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
