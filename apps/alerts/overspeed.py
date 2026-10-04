"""OVER SPEEDING alert events (HIGH), from the device's own overspeed event.

The mapping (website side, the only place it lives)
---------------------------------------------------
comms stores, per reading, ``eventioval``: the id of the I/O element whose
change made the device send that record. Teltonika's Over Speeding I/O is 255:

    eventioval == 255   ->  overspeed = 1   (this record IS an overspeed event)
    eventioval other    ->  overspeed = 0
    eventioval NULL     ->  no information (older rows / other providers)

``overspeed_from_eventioval`` is that mapping; the comms bridge applies it and
stores only the result, ``metadata["overspeed"]`` — ``eventioval`` itself is
not kept and never shown.

One alert per overspeed EPISODE
-------------------------------
Unlike panic (a level that stays 1 while held), overspeed is EVENT-driven: the
device sends one 255 record when the vehicle goes over the limit and another
when it drops back under, with ordinary records (overspeed 0) in between —
seen in this fleet's data, e.g. 12:31:12 at 63 km/h, then 12:31:23 at 55 km/h.
So per vehicle, in device time:

    overspeed=1 with no open episode   -> START: create ONE alert (+ notification)
    overspeed=1 while an episode is open -> END: stamp signal_cleared_at; the
                                           alert's speed becomes the top speed
    overspeed=0                         -> nothing (ordinary record)

An episode only pairs with a later 255 that is within
``OVERSPEED_MAX_EPISODE_MINUTES`` and with no ignition OFF in between;
otherwise that 255 starts a new episode (the end record was lost) and the
stale episode is closed at its last known reading. An END that arrives before
its START (comms delivers out of order) is repaired when the START arrives.
Re-delivered records are no-ops. Runs under the per-vehicle lock, like panic.
"""

import datetime

from django.conf import settings
from django.db.models import Max
from django.urls import reverse

from apps.alerts.models import Alert

OVERSPEED_EVENT_IO = 255
OVERSPEED_KEY = "overspeed"


def overspeed_from_eventioval(eventioval):
    """The website's mapping of the device event id to the overspeed flag."""
    if eventioval in (None, ""):
        return None
    try:
        return 1 if int(eventioval) == OVERSPEED_EVENT_IO else 0
    except (TypeError, ValueError):
        return None


def overspeed_signal(metadata):
    value = (metadata or {}).get(OVERSPEED_KEY)
    if value in (None, ""):
        return None
    try:
        return 1 if int(value) == 1 else 0
    except (TypeError, ValueError):
        return None


def _max_episode():
    return datetime.timedelta(minutes=getattr(settings, "OVERSPEED_MAX_EPISODE_MINUTES", 30))


def _history(vehicle):
    from apps.tracking.models import TelemetryEvent

    return TelemetryEvent.objects.filter(vehicle=vehicle)


def _can_pair(vehicle, start, end):
    """Can a 255 at ``end`` close the episode that started at ``start``?"""
    if end <= start or end - start > _max_episode():
        return False
    return not _history(vehicle).filter(timestamp__gt=start, timestamp__lt=end, ignition=False).exists()


def _top_speed(vehicle, start, end, *candidates):
    stored = _history(vehicle).filter(timestamp__gte=start, timestamp__lte=end).aggregate(top=Max("speed"))["top"]
    values = [v for v in (stored, *candidates) if v is not None]
    return max(values) if values else None


def _close(alert, vehicle, end_reading):
    alert.signal_cleared_at = end_reading.timestamp
    alert.last_signal_at = end_reading.timestamp
    alert.speed = _top_speed(vehicle, alert.occurred_at, end_reading.timestamp, alert.speed, end_reading.speed)
    alert.save(update_fields=["signal_cleared_at", "last_signal_at", "speed", "updated_at"])


def _on_event(vehicle, device, reading, *, notify):
    """(alert, created?) for one overspeed (255) record."""
    from apps.alerts.events import _snapshot, _source_record

    t = reading.timestamp
    events = Alert.objects.filter(category=Alert.Category.OVER_SPEEDING, vehicle=vehicle)
    if events.filter(occurred_at=t).exists() or events.filter(signal_cleared_at=t).exists():
        return None, False  # re-delivered record

    previous = events.filter(occurred_at__lt=t).order_by("-occurred_at").first()
    if previous is not None and previous.signal_cleared_at is not None and t < previous.signal_cleared_at:
        return previous, False  # inside an episode we already have
    if previous is not None and previous.signal_cleared_at is None:
        if _can_pair(vehicle, previous.occurred_at, t):
            _close(previous, vehicle, reading)  # this record is the END of the open episode
            return previous, False
        # The end record never came: close the stale episode at its last known reading.
        previous.signal_cleared_at = previous.last_signal_at or previous.occurred_at
        previous.save(update_fields=["signal_cleared_at", "updated_at"])

    following = events.filter(occurred_at__gt=t).order_by("occurred_at").first()
    if (following is not None and following.signal_cleared_at is None
            and _can_pair(vehicle, t, following.occurred_at)):
        # Out of order: the alert we have started at what was really the END record.
        end_at, end_speed = following.occurred_at, following.speed
        fields = _snapshot(reading, _source_record(device, reading))
        fields.pop("voltage", None)
        for name, value in fields.items():
            setattr(following, name, value)
        following.triggered_at = t
        following.signal_cleared_at = following.last_signal_at = end_at
        following.speed = _top_speed(vehicle, t, end_at, reading.speed, end_speed)
        following.save(update_fields=[*fields, "triggered_at", "signal_cleared_at", "last_signal_at", "updated_at"])
        return following, False

    fields = _snapshot(reading, _source_record(device, reading))
    fields.pop("voltage", None)
    alert = Alert.objects.create(
        dedupe_key=f"OVER_SPEEDING:{vehicle.pk}:{t.isoformat()}",
        category=Alert.Category.OVER_SPEEDING,
        severity=Alert.Severity.HIGH,
        status=Alert.Status.OPEN,
        title=f"Over Speeding alert — {vehicle.registration_number}",
        vehicle=vehicle,
        client_id=vehicle.client_id,
        driver_id=vehicle.current_driver_id,
        triggered_at=t,
        last_signal_at=t,
        **fields,
    )
    alert.message = f"Over speeding at {float(alert.speed):.0f} km/h." if alert.speed is not None else "Over speeding."
    alert.link_url = f"{reverse('alerts:alert_report')}?alert={alert.uuid}"
    alert.save(update_fields=["message", "link_url", "updated_at"])
    if notify:
        from apps.alerts.events import _is_recent, notify_alert

        if _is_recent(t):
            notify_alert(alert)
    return alert, True


def process_vehicle_overspeed(*, vehicle, device, readings, notify=True):
    """Apply a batch's overspeed records (already stored) to the vehicle's
    alerts. Returns the alerts CREATED. Caller holds the lock + transaction."""
    marks = sorted((r for r in readings if overspeed_signal(r.metadata) == 1), key=lambda r: r.timestamp)
    created = []
    for reading in marks:
        alert, is_new = _on_event(vehicle, device, reading, notify=notify)
        if is_new:
            created.append(alert)
    return created
