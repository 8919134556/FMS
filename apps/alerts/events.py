"""Vehicle alert EVENTS from the GPS history — the one place telemetry
alerts are detected: the LEVEL alerts here (PANIC and MAIN_POWER_DISCONNECTED,
one shared episode engine), IDLE in apps.alerts.idle, OVER_SPEEDING in
apps.alerts.overspeed, GEOFENCE_* in apps.geofences.services, all driven by
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

Main power (same engine)
------------------------
comms stores ``mainpower`` = externalvoltage / 1000 (volts). The bridge
classifies it with ``main_power_state`` (exact ranges: 0 <= V < 5 disconnected,
5 <= V < 8.5 low, V >= 8.5 normal; NULL/negative = no information) and stores
two flags, ``power_cut`` and ``low_voltage``; the MAIN_POWER (HIGH) and
LOW_VOLTAGE (MEDIUM) specs below raise "Main Power Disconnected" / "Low
Voltage" on 0 -> 1, exactly like panic — so Normal -> Low -> Disconnected ->
Normal produces one Low alert (cleared when it drops below 5 V), one
Disconnected alert (cleared at >= 8.5 V, or when it rises to the low range).

Device battery (same engine, separate flags)
--------------------------------------------
comms stores ``device_battery_voltage`` = batteryvoltage / 1000 (V). The same
classifier on DEVICE_BATTERY_BANDS (0 <= V < 2 disconnected, 2 <= V < 3 low,
V >= 3 normal) sets ``battery_cut`` / ``battery_low``, raising "Device
Battery Disconnected" (HIGH) / "Device Battery Low Voltage" (MEDIUM). It is an
alert input only: never shown on Live Tracking, never on the current position. State lives in
the Alert rows, so a restart (or the bridge re-delivering a 0 V reading) is a
no-op inside the existing episode — never a repeated alert.

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

import dataclasses
import logging
from decimal import Decimal, InvalidOperation

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
    return level_signal(metadata, PANIC_KEY)


MAINPOWER_KEY = "mainpower"  # main power voltage (V), as comms stores it
POWER_CUT_KEY = "power_cut"  # 1 = main power disconnected (< 5 V), else 0
LOW_VOLTAGE_KEY = "low_voltage"  # 1 = low voltage (5 V to < 8.5 V), else 0

DEVICE_BATTERY_KEY = "device_battery_voltage"  # device battery voltage (V), as comms stores it
BATTERY_CUT_KEY = "battery_cut"  # 1 = device battery disconnected (< 2 V), else 0
BATTERY_LOW_KEY = "battery_low"  # 1 = device battery low (2 V to < 3 V), else 0

POWER_DISCONNECTED, POWER_LOW, POWER_NORMAL = "DISCONNECTED", "LOW", "NORMAL"


@dataclasses.dataclass(frozen=True)
class VoltageBands:
    """Exact, non-overlapping voltage ranges for one supply:
    0 <= V < disconnected_below -> DISCONNECTED, disconnected_below <= V <
    low_below -> LOW, V >= low_below -> NORMAL. Each band has its own pair
    of reading flags, so the supplies never share state."""

    disconnected_below: Decimal
    low_below: Decimal
    disconnected_key: str
    low_key: str


# Main power (comms mainpower = externalvoltage / 1000): 0-<5 V disconnected, 5-<8.5 V low.
MAIN_POWER_BANDS = VoltageBands(Decimal("5"), Decimal("8.5"), POWER_CUT_KEY, LOW_VOLTAGE_KEY)
# Device battery (comms device_battery_voltage = batteryvoltage / 1000): 0-<2 V disconnected, 2-<3 V low.
DEVICE_BATTERY_BANDS = VoltageBands(Decimal("2"), Decimal("3"), BATTERY_CUT_KEY, BATTERY_LOW_KEY)
DISCONNECTED_BELOW_V = MAIN_POWER_BANDS.disconnected_below
LOW_VOLTAGE_BELOW_V = MAIN_POWER_BANDS.low_below


def level_signal(metadata, key):
    """1 / 0 for a level flag in a reading's metadata, None when it says nothing."""
    value = (metadata or {}).get(key)
    if value in (None, ""):
        return None
    try:
        return 1 if int(value) == 1 else 0
    except (TypeError, ValueError):
        return None


def voltage_state(bands, value):
    """DISCONNECTED / LOW / NORMAL for a voltage (V) on ``bands``. NULL,
    unreadable or negative values -> None: no information, never an alert
    (comms divides an unsigned millivolt count by 1000, so a negative value is
    bad data). Compared as Decimal so boundaries like 4.99 / 5.00 / 8.49 /
    8.50 or 1.99 / 2.00 / 2.99 / 3.00 fall exactly as specified."""
    if value in (None, ""):
        return None
    try:
        volts = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not volts.is_finite() or volts < 0:
        return None
    if volts < bands.disconnected_below:
        return POWER_DISCONNECTED
    if volts < bands.low_below:
        return POWER_LOW
    return POWER_NORMAL


def voltage_flags(bands, value):
    """The two level flags a reading carries for ``bands`` ({} when the
    voltage says nothing). Exactly one is 1 unless NORMAL."""
    state = voltage_state(bands, value)
    if state is None:
        return {}
    return {bands.disconnected_key: int(state == POWER_DISCONNECTED), bands.low_key: int(state == POWER_LOW)}


def main_power_state(mainpower):
    """Main power: 0 <= V < 5 DISCONNECTED, 5 <= V < 8.5 LOW, V >= 8.5 NORMAL."""
    return voltage_state(MAIN_POWER_BANDS, mainpower)


def main_power_flags(mainpower):
    return voltage_flags(MAIN_POWER_BANDS, mainpower)


def device_battery_state(volts):
    """Device battery: 0 <= V < 2 DISCONNECTED, 2 <= V < 3 LOW, V >= 3 NORMAL."""
    return voltage_state(DEVICE_BATTERY_BANDS, volts)


def device_battery_flags(volts):
    return voltage_flags(DEVICE_BATTERY_BANDS, volts)


def power_cut_from_mainpower(mainpower):
    """1 when the voltage is in the disconnected range, 0 otherwise, None when unknown."""
    return main_power_flags(mainpower).get(POWER_CUT_KEY)


@dataclasses.dataclass(frozen=True)
class LevelAlert:
    """An alert raised while a per-reading LEVEL flag is 1 (and on every
    reading the device reports it): 0 -> 1 opens ONE alert, 1 -> 1 continues
    it, 1 -> 0 clears it, 0 -> 1 later opens a new one. See the module doc."""

    category: str
    key: str  # metadata flag
    severity: str
    voltage_key: str = ""  # metadata value stored in Alert.voltage
    message: str = ""  # Alert.message ("" = the location); may use {voltage}


PANIC = LevelAlert(Alert.Category.PANIC, PANIC_KEY, Alert.Severity.CRITICAL, voltage_key=PANIC_VOLTAGE_KEY)
# Low -> Disconnected (or back) clears one flag and raises the other: the
# NORMAL / LOW_VOLTAGE / MAIN_POWER_DISCONNECTED state machine per vehicle.
MAIN_POWER = LevelAlert(Alert.Category.MAIN_POWER_DISCONNECTED, POWER_CUT_KEY, Alert.Severity.HIGH,
                        voltage_key=MAINPOWER_KEY, message="Main power disconnected ({voltage} V).")
LOW_VOLTAGE = LevelAlert(Alert.Category.LOW_VOLTAGE, LOW_VOLTAGE_KEY, Alert.Severity.MEDIUM,
                         voltage_key=MAINPOWER_KEY, message="Low main power voltage ({voltage} V).")
# Device battery: same state machine on its own flags. The voltage is kept on
# the alert (report / exports) but deliberately NOT in the message, so the
# notification popup — which can open over Live Tracking — never shows it.
DEVICE_BATTERY_CUT = LevelAlert(Alert.Category.DEVICE_BATTERY_DISCONNECTED, BATTERY_CUT_KEY, Alert.Severity.HIGH,
                                voltage_key=DEVICE_BATTERY_KEY, message="Device battery disconnected.")
DEVICE_BATTERY_LOW = LevelAlert(Alert.Category.DEVICE_BATTERY_LOW_VOLTAGE, BATTERY_LOW_KEY, Alert.Severity.MEDIUM,
                                voltage_key=DEVICE_BATTERY_KEY, message="Device battery low voltage.")
LEVEL_ALERTS = (PANIC, MAIN_POWER, LOW_VOLTAGE, DEVICE_BATTERY_CUT, DEVICE_BATTERY_LOW)


def _lock_vehicle(vehicle_id):
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [_LOCK_NAMESPACE, vehicle_id])


def _history(vehicle):
    from apps.tracking.models import TelemetryEvent

    return TelemetryEvent.objects.filter(vehicle=vehicle)


def _zero_between(vehicle, after, before, key=PANIC_KEY):
    """Is there a "normal" (flag 0) reading strictly between the two times?"""
    return _history(vehicle).filter(timestamp__gt=after, timestamp__lt=before, **{f"metadata__{key}": 0}).exists()


def _first_zero_after(vehicle, when, key=PANIC_KEY):
    return (
        _history(vehicle).filter(timestamp__gt=when, **{f"metadata__{key}": 0})
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


def _snapshot(reading, source, voltage_key=PANIC_VOLTAGE_KEY):
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
        "voltage": _decimal_or_none(metadata.get(voltage_key)) if voltage_key else None,
    }


def _source_record(device, reading):
    from apps.tracking.models import TelemetryEvent

    if isinstance(reading, TelemetryEvent):
        return reading
    return TelemetryEvent.objects.filter(
        device=device, timestamp=reading.timestamp, latitude=reading.latitude, longitude=reading.longitude
    ).first()


def _on_level(spec, vehicle, device, reading, *, notify):
    """(alert this flag-1 reading belongs to, created?)."""
    t = reading.timestamp
    events = Alert.objects.filter(category=spec.category, vehicle=vehicle)

    previous = events.filter(occurred_at__lte=t).order_by("-occurred_at").first()
    if previous is not None:
        last = previous.last_signal_at or previous.occurred_at
        if t <= last:
            return previous, False  # inside the episode (duplicate / late reading): nothing new
        if not _zero_between(vehicle, last, t, spec.key):
            previous.last_signal_at = t
            previous.save(update_fields=["last_signal_at", "updated_at"])
            return previous, False

    following = events.filter(occurred_at__gt=t).order_by("occurred_at").first()
    if following is not None and not _zero_between(vehicle, t, following.occurred_at, spec.key):
        # A late reading from just before a known episode: the episode started earlier.
        fields = _snapshot(reading, _source_record(device, reading), spec.voltage_key)
        fields["triggered_at"] = t
        for name, value in fields.items():
            setattr(following, name, value)
        following.save(update_fields=[*fields, "updated_at"])
        return following, False

    alert = Alert.objects.create(
        dedupe_key=f"{spec.category}:{vehicle.pk}:{t.isoformat()}",
        category=spec.category,
        severity=spec.severity,
        status=Alert.Status.OPEN,
        title=f"{Alert.Category(spec.category).label} alert — {vehicle.registration_number}",
        vehicle=vehicle,
        client_id=vehicle.client_id,
        driver_id=vehicle.current_driver_id,
        triggered_at=t,  # a level alert is raised the moment the flag goes to 1
        last_signal_at=t,
        signal_cleared_at=_first_zero_after(vehicle, t, spec.key),
        **_snapshot(reading, _source_record(device, reading), spec.voltage_key),
    )
    voltage = f"{alert.voltage:.2f}" if alert.voltage is not None else "?"
    alert.message = spec.message.format(voltage=voltage) if spec.message else alert.location
    alert.link_url = f"{reverse('alerts:alert_report')}?alert={alert.uuid}"
    alert.save(update_fields=["message", "link_url", "updated_at"])
    if notify:
        notify_alert(alert)
    return alert, True


def _on_normal(spec, vehicle, reading):
    t = reading.timestamp
    episode = (
        Alert.objects.filter(category=spec.category, vehicle=vehicle, occurred_at__lt=t)
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


def process_vehicle_signals(*, vehicle, device, readings, notify=True, specs=None):
    """Apply a batch of a vehicle's readings (already saved to TelemetryEvent
    history) to its LEVEL alerts (panic, main power). Returns the alerts
    CREATED by this batch.

    Must run inside a transaction (ingestion's own); takes the per-vehicle lock."""
    if vehicle is None:
        return []
    created = []
    for spec in specs or LEVEL_ALERTS:
        signals = sorted(
            ((r, s) for r in readings if (s := level_signal(r.metadata, spec.key)) is not None),
            key=lambda pair: pair[0].timestamp,
        )
        if not signals:
            continue
        _lock_vehicle(vehicle.pk)
        for reading, signal in signals:
            if signal == 1:
                alert, is_new = _on_level(spec, vehicle, device, reading,
                                          notify=notify and _is_recent(reading.timestamp))
                if is_new:
                    created.append(alert)
            else:
                _on_normal(spec, vehicle, reading)
    return created


def evaluate_ingested(*, vehicle, device, readings):
    """Ingestion hook (apps.tracking.services): every telemetry alert type for
    the batch just stored. Each detector runs in its own savepoint under the
    per-vehicle lock and never raises — an alerting bug must not lose the
    telemetry itself, nor stop the other alert type."""
    from apps.alerts import idle, overspeed
    from apps.geofences.services import process_vehicle_geofences

    if vehicle is None or not readings:
        return []
    created = []
    detectors = [("idle", lambda: idle.process_vehicle_idle(vehicle=vehicle, readings=readings)),
                 ("geofence", lambda: process_vehicle_geofences(vehicle=vehicle, readings=readings))]
    if any(overspeed.overspeed_signal(r.metadata) == 1 for r in readings):
        detectors.insert(0, ("overspeed", lambda: overspeed.process_vehicle_overspeed(
            vehicle=vehicle, device=device, readings=readings)))
    if any(level_signal(r.metadata, spec.key) is not None for r in readings for spec in LEVEL_ALERTS):
        detectors.insert(0, ("panic / main power", lambda: process_vehicle_signals(
            vehicle=vehicle, device=device, readings=readings)))
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

    parts = [f"Level: {alert.get_severity_display()}"]
    if alert.category != Alert.Category.PANIC and alert.message:  # panic's message is its location (below)
        parts.append(alert.message)
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
