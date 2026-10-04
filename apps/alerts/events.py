"""Vehicle alert EVENTS from the GPS history — the one place telemetry
alerts are detected: PANIC here, IDLE in apps.alerts.idle, both driven by
``evaluate_ingested`` for every ingested batch. The Alert Report, the bell
notification + sound, and the PDF/Excel exports all read the Alert rows
written here; none of them looks at the raw signal again.

Source of truth for panic
-------------------------
The device's panic input is evaluated upstream, by the comms program's
stored procedure ``insert_current_and_history_json``: voltage = analog1 / 1000,
``panic = 1`` when that voltage is >= 10 V, else 0. The comms bridge
(apps.tracking.comms_sync) copies that value, unchanged, into each history
record's ``metadata["panic"]`` (and the analog voltage, when the comms Raw DB
is configured, into ``metadata["panic_voltage"]``). Nothing here recomputes it.
A reading without the key (older data, other providers) carries no panic
information and never changes state.

One alert per EPISODE, per vehicle
----------------------------------
Readings are judged in DEVICE time order, not arrival order (comms inserts
late/out-of-order rows; a clearing 0 can even arrive before the 1 it clears):

    0 -> 1   a new episode: create ONE alert (+ notification, once)
    1 -> 1   the same episode: extend it, no new alert, no new notification
    1 -> 0   the episode is cleared: stamp ``signal_cleared_at`` (kept in history)
    0 -> 0   nothing

Concretely, a panic reading at time t belongs to an existing episode when no
0-reading lies between it and that episode (before or after t, so a late
reading that predates the episode just moves its start back). Otherwise it
starts a new episode. Re-delivered readings fall inside an existing episode
and are no-ops, so processing is idempotent. All of this runs under a
per-vehicle advisory lock inside the ingestion transaction, so concurrent
ingestion for one vehicle can never create two alerts for one episode.
"""

import logging

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.alerts.models import Alert

logger = logging.getLogger(__name__)

PANIC_KEY = "panic"
PANIC_VOLTAGE_KEY = "panic_voltage"
# pg_advisory_xact_lock(namespace, vehicle_id): "AL" — distinct from the comms bridge's lock.
_LOCK_NAMESPACE = 0x414C


def panic_signal(metadata):
    """1 / 0 from a reading's metadata, or None when the reading says nothing."""
    value = (metadata or {}).get(PANIC_KEY)
    if value in (None, ""):
        return None
    try:
        return 1 if int(value) == 1 else 0
    except (TypeError, ValueError):
        return None


def _lock_vehicle(vehicle_id):
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [_LOCK_NAMESPACE, vehicle_id])


def _history(vehicle):
    from apps.tracking.models import TelemetryEvent

    return TelemetryEvent.objects.filter(vehicle=vehicle)


def _zero_between(vehicle, after, before):
    """Is there a "signal normal" (0) reading strictly between the two times?"""
    return _history(vehicle).filter(timestamp__gt=after, timestamp__lt=before, metadata__panic=0).exists()


def _first_zero_after(vehicle, when):
    return (
        _history(vehicle).filter(timestamp__gt=when, metadata__panic=0)
        .order_by("timestamp").values_list("timestamp", flat=True).first()
    )


def _decimal_or_none(value):
    from decimal import Decimal, InvalidOperation

    if value in (None, ""):
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _snapshot(reading, source):
    """Alert fields describing the reading that raised (or now starts) the episode."""
    from apps.tracking.trip_report import _has_gps_fix

    metadata = reading.metadata or {}
    has_fix = _has_gps_fix(reading.latitude, reading.longitude)
    return {
        "occurred_at": reading.timestamp,
        "telemetry_event": source,
        "latitude": reading.latitude if has_fix else None,
        "longitude": reading.longitude if has_fix else None,
        "location": (metadata.get("location") or "")[:255],
        "speed": reading.speed,
        "ignition": reading.ignition,
        "odometer": reading.odometer,
        "voltage": _decimal_or_none(metadata.get(PANIC_VOLTAGE_KEY)),
    }


def _source_record(device, reading):
    from apps.tracking.models import TelemetryEvent

    if isinstance(reading, TelemetryEvent):
        return reading
    return TelemetryEvent.objects.filter(
        device=device, timestamp=reading.timestamp, latitude=reading.latitude, longitude=reading.longitude
    ).first()


def _on_panic(vehicle, device, reading, *, notify):
    """(alert this panic reading belongs to, created?)."""
    t = reading.timestamp
    events = Alert.objects.filter(category=Alert.Category.PANIC, vehicle=vehicle)

    previous = events.filter(occurred_at__lte=t).order_by("-occurred_at").first()
    if previous is not None:
        last = previous.last_signal_at or previous.occurred_at
        if t <= last:
            return previous, False  # inside the episode (duplicate / late reading): nothing new
        if not _zero_between(vehicle, last, t):
            previous.last_signal_at = t
            previous.save(update_fields=["last_signal_at", "updated_at"])
            return previous, False

    following = events.filter(occurred_at__gt=t).order_by("occurred_at").first()
    if following is not None and not _zero_between(vehicle, t, following.occurred_at):
        # A late reading from just before a known episode: the episode started earlier.
        fields = _snapshot(reading, _source_record(device, reading))
        fields["triggered_at"] = t
        for name, value in fields.items():
            setattr(following, name, value)
        following.save(update_fields=[*fields, "updated_at"])
        return following, False

    alert = Alert.objects.create(
        dedupe_key=f"PANIC:{vehicle.pk}:{t.isoformat()}",
        category=Alert.Category.PANIC,
        severity=Alert.Severity.CRITICAL,
        status=Alert.Status.OPEN,
        title=f"Panic alert — {vehicle.registration_number}",
        vehicle=vehicle,
        client_id=vehicle.client_id,
        driver_id=vehicle.current_driver_id,
        triggered_at=t,  # a panic alerts the moment it starts
        last_signal_at=t,
        signal_cleared_at=_first_zero_after(vehicle, t),
        **_snapshot(reading, _source_record(device, reading)),
    )
    alert.message = alert.location
    alert.link_url = f"{reverse('alerts:alert_report')}?alert={alert.uuid}"
    alert.save(update_fields=["message", "link_url", "updated_at"])
    if notify:
        notify_alert(alert)
    return alert, True


def _on_clear(vehicle, reading):
    t = reading.timestamp
    episode = (
        Alert.objects.filter(category=Alert.Category.PANIC, vehicle=vehicle, occurred_at__lt=t)
        .order_by("-occurred_at").first()
    )
    if episode is None:
        return
    last = episode.last_signal_at or episode.occurred_at
    # A 0 timestamped INSIDE an episode we already reported is ignored rather
    # than splitting it after the fact; the earliest 0 after it clears it.
    if t > last and (episode.signal_cleared_at is None or t < episode.signal_cleared_at):
        episode.signal_cleared_at = t
        episode.save(update_fields=["signal_cleared_at", "updated_at"])


def process_vehicle_signals(*, vehicle, device, readings, notify=True):
    """Apply a batch of a vehicle's readings (already saved to TelemetryEvent
    history) to its alert state. Returns the alerts CREATED by this batch.

    Must run inside a transaction (ingestion's own); takes the per-vehicle lock."""
    signals = sorted(
        ((r, s) for r in readings if (s := panic_signal(r.metadata)) is not None),
        key=lambda pair: pair[0].timestamp,
    )
    if not signals or vehicle is None:
        return []
    _lock_vehicle(vehicle.pk)
    created = []
    for reading, signal in signals:
        if signal == 1:
            alert, is_new = _on_panic(vehicle, device, reading, notify=notify and _is_recent(reading.timestamp))
            if is_new:
                created.append(alert)
        else:
            _on_clear(vehicle, reading)
    return created


def evaluate_ingested(*, vehicle, device, readings):
    """Ingestion hook (apps.tracking.services): every telemetry alert type for
    the batch just stored. Each detector runs in its own savepoint under the
    per-vehicle lock and never raises — an alerting bug must not lose the
    telemetry itself, nor stop the other alert type."""
    from apps.alerts import idle

    if vehicle is None or not readings:
        return []
    created = []
    detectors = [("idle", lambda: idle.process_vehicle_idle(vehicle=vehicle, readings=readings))]
    if any(panic_signal(r.metadata) is not None for r in readings):
        detectors.insert(0, ("panic", lambda: process_vehicle_signals(vehicle=vehicle, device=device, readings=readings)))
    for name, run in detectors:
        try:
            with transaction.atomic():
                _lock_vehicle(vehicle.pk)
                created += run()
        except Exception:
            logger.exception("%s alert evaluation failed for vehicle %s", name.capitalize(), vehicle.pk)
    return created


# ---------------------------------------------------------------------------
# Notification (bell + popup + sound) — once per NEW event
# ---------------------------------------------------------------------------


def _is_recent(when):
    """A first-time import of old history must not page everyone about panics
    from last week: only events newer than ALERT_NOTIFY_MAX_AGE_MINUTES notify."""
    max_age = timezone.timedelta(minutes=getattr(settings, "ALERT_NOTIFY_MAX_AGE_MINUTES", 1440))
    return when >= timezone.now() - max_age


def alert_recipients(alert):
    """Active users allowed to see this alert: RBAC ``alert.view`` (superusers
    always) AND the vehicle's client in their scope (internal staff, or a
    client user of that same client) — the same rules as the Alert Report."""
    from apps.accounts.models import User

    users = User.objects.filter(is_active=True, is_deleted=False).filter(
        Q(is_superuser=True)
        | Q(role__is_active=True, role__permissions__module="alert", role__permissions__action="view")
    )
    users = users.filter(Q(is_superuser=True) | Q(client__isnull=True) | Q(client_id=alert.client_id))
    return list(users.distinct())


def notification_body(alert):
    from apps.core.utils import display_timezone

    parts = [alert.message] if alert.category == Alert.Category.IDLE and alert.message else []
    if alert.driver_id:
        parts.append(f"Driver: {alert.driver.get_full_name()}")
    when = alert.triggered_at or alert.occurred_at
    if when:
        parts.append(when.astimezone(display_timezone()).strftime("%d %b %Y %H:%M:%S"))
    if alert.location:
        parts.append(alert.location)
    elif alert.latitude is not None:
        parts.append(f"{alert.latitude}, {alert.longitude}")
    return " · ".join(parts)[:500]


def notify_alert(alert):
    from apps.notifications.models import Notification
    from apps.notifications.services import notify_many

    level = {
        Alert.Severity.CRITICAL: Notification.Level.CRITICAL, Alert.Severity.HIGH: Notification.Level.WARNING,
        Alert.Severity.MEDIUM: Notification.Level.WARNING,
    }.get(alert.severity, Notification.Level.INFO)
    recipients = alert_recipients(alert)
    if recipients:
        notify_many(
            recipients,
            title=f"{alert.get_category_display()} alert — {alert.vehicle.registration_number}",
            body=notification_body(alert),
            level=level,
            link_url=alert.link_url,
            alert=alert,
        )
    return recipients
