"""IDLE alert events: ignition ON + the vehicle genuinely stationary,
continuously, for the vehicle's idle threshold (``Vehicle.idle_alert_minutes``,
default 5). One MEDIUM ``Alert`` (category IDLE) per idle episode; the Alert
Report, the bell notification + sound and the PDF/Excel exports all read that
row. Independent of trips: nothing here opens, closes or reads a trip.

What "stationary" means
-----------------------
Never a single ``speed == 0``. The vehicle's own movement settings — the ones
trip detection already uses (``trip_min_speed_kmh``, ``trip_min_distance_m``)
— define movement, so trips and idle never disagree about it. Against the
spot where the idle run began (its first trustworthy GPS fix), a reading shows
MOVEMENT when:

  * it is beyond the distance AND that is corroborated — the next reading is
    still beyond it, or the speed is at/above the threshold (one GPS jump or
    drift spike is not movement; drift while parked is well inside 50 m), or
  * the speed is at/above the threshold on two consecutive readings (one speed
    spike — 0, 0, 2, 0, 0 or even 0, 18, 0 — is not movement), or
  * on a reading WITHOUT a usable fix (0,0 / device no-fix), the odometer
    advanced at least the distance (and at least 0.2 km, two of the device's
    0.1 km steps) at a plausible speed. With a good fix the position decides:
    on these devices the odometer creeps while parked with the engine on (e.g.
    +0.2 km in 24 min within 23 m) and can jump when its counter is set.

State machine (per vehicle, in DEVICE time order)
-------------------------------------------------
    MOVING ──ignition ON reading──▶ CANDIDATE (idle timer starts here)
    CANDIDATE ──movement / ignition OFF / data gap──▶ MOVING (timer reset, no alert)
    CANDIDATE ──stationary ≥ threshold, ≥ N readings──▶ CONFIRMED  → one Alert (+ notification)
    CONFIRMED ──more stationary readings──▶ CONFIRMED (same Alert: last reading / duration grow)
    CONFIRMED ──movement / ignition OFF / data gap──▶ ENDED (Alert end time set) ▶ MOVING

A gap longer than ``IDLE_ALERT_MAX_GAP_SECONDS`` between readings ends the run
at the last reading before it: missing data is never assumed to be idling.

Out-of-order, late and duplicate readings
-----------------------------------------
The machine is re-run over the vehicle's stored history around each ingested
batch (from the open idle episode, or from ``threshold + max gap`` before the
batch) until it is back in MOVING, and the episodes it finds are reconciled
with existing IDLE alerts by time overlap — an existing alert is updated,
never duplicated. Re-delivering readings changes nothing. It runs under the
same per-vehicle advisory lock as the panic engine.
"""

import dataclasses
import datetime

from django.conf import settings
from django.db.models import Q
from django.urls import reverse

from apps.alerts.models import Alert

IDLE_TITLE = "Idle alert — {registration}"


@dataclasses.dataclass(frozen=True)
class IdleConfig:
    threshold: datetime.timedelta
    min_speed_kmh: float
    radius_m: float
    max_gap: datetime.timedelta
    min_readings: int

    @classmethod
    def for_vehicle(cls, vehicle):
        from apps.vehicles import models as vm

        def setting(name, default, low):
            value = getattr(vehicle, name, None)
            return value if isinstance(value, int) and value >= low else default

        return cls(
            threshold=datetime.timedelta(minutes=setting("idle_alert_minutes", vm.DEFAULT_IDLE_ALERT_MINUTES, 1)),
            min_speed_kmh=setting("trip_min_speed_kmh", vm.DEFAULT_TRIP_MIN_SPEED_KMH, 1),
            radius_m=setting("trip_min_distance_m", vm.DEFAULT_TRIP_MIN_DISTANCE_M, 1),
            max_gap=datetime.timedelta(seconds=getattr(settings, "IDLE_ALERT_MAX_GAP_SECONDS", 180)),
            min_readings=max(1, getattr(settings, "IDLE_ALERT_MIN_READINGS", 3)),
        )


@dataclasses.dataclass
class IdleEpisode:
    """One stationary run with the ignition ON (the machine's CANDIDATE or
    CONFIRMED state). ``anchor`` is the spot it is measured from."""

    start: object  # first reading of the run
    last: object  # latest reading still stationary
    anchor: object = None  # first trustworthy fix of the run
    origin_odometer: object = None
    readings: int = 1
    triggered_at: datetime.datetime = None  # set when CONFIRMED
    end_at: datetime.datetime = None  # set when ENDED
    end_reason: str = ""

    @property
    def confirmed(self):
        return self.triggered_at is not None


class IdleMachine:
    MOVING, CANDIDATE, CONFIRMED = "MOVING", "CANDIDATE", "CONFIRMED"

    def __init__(self, config, seed=None):
        self.config = config
        self.run = seed  # IdleEpisode or None
        self.pending_far = None  # a reading beyond the radius, awaiting corroboration
        self.pending_fast = None  # a reading at/above the speed, awaiting corroboration
        self.episodes = []  # CONFIRMED episodes that ENDED

    @property
    def state(self):
        if self.run is None:
            return self.MOVING
        return self.CONFIRMED if self.run.confirmed else self.CANDIDATE

    # -- signals --

    def _fix_distance(self, row):
        from apps.tracking.trip_report import _distance_m, _trustworthy_fix

        anchor = self.run.anchor
        if anchor is None or not _trustworthy_fix(row):
            return None
        return _distance_m(anchor.latitude, anchor.longitude, row.latitude, row.longitude)

    def _plausible(self, row, metres):
        from apps.tracking.trip_report import MAX_PLAUSIBLE_SPEED_KMH

        hours = (row.timestamp - self.run.anchor.timestamp).total_seconds() / 3600
        return hours > 0 and (metres / 1000) / hours <= MAX_PLAUSIBLE_SPEED_KMH

    def _odometer_moved(self, row):
        """Only consulted when the reading has no usable fix (see module doc)."""
        from apps.tracking.trip_report import MAX_PLAUSIBLE_SPEED_KMH, ODOMETER_ONLY_MIN_KM

        if self.run.origin_odometer is None or row.odometer is None:
            return False
        advanced = row.odometer - self.run.origin_odometer
        if advanced < ODOMETER_ONLY_MIN_KM or float(advanced) * 1000 < self.config.radius_m:
            return False
        hours = (row.timestamp - self.run.start.timestamp).total_seconds() / 3600
        return hours > 0 and float(advanced) / hours <= MAX_PLAUSIBLE_SPEED_KMH

    # -- transitions --

    def _start(self, row):
        from apps.tracking.trip_report import _trustworthy_fix

        self.run = IdleEpisode(start=row, last=row, anchor=row if _trustworthy_fix(row) else None,
                               origin_odometer=row.odometer)
        self.pending_far = self.pending_fast = None
        self._maybe_confirm()

    def _end(self, at, reason):
        if self.run is not None and self.run.confirmed:
            self.run.end_at, self.run.end_reason = at, reason
            self.episodes.append(self.run)
        self.run = None
        self.pending_far = self.pending_fast = None

    def _maybe_confirm(self):
        run = self.run
        if (not run.confirmed and run.readings >= self.config.min_readings
                and run.last.timestamp - run.start.timestamp >= self.config.threshold):
            run.triggered_at = run.last.timestamp

    def feed(self, row):
        from apps.tracking.trip_report import _trustworthy_fix

        if self.run is not None and row.timestamp - self.run.last.timestamp > self.config.max_gap:
            self._end(self.run.last.timestamp, "no data")  # never assume idling across missing data
        if row.ignition is not True:
            self._end(row.timestamp, "ignition off")
            return
        if self.run is None:
            self._start(row)
            return
        metres = self._fix_distance(row)
        far = metres is not None and metres > self.config.radius_m and self._plausible(row, metres)
        fast = row.speed is not None and float(row.speed) >= self.config.min_speed_kmh
        no_fix = not _trustworthy_fix(row)
        moving = (
            (no_fix and self._odometer_moved(row))
            or (far and (fast or self.pending_far is not None))
            or (fast and self.pending_fast is not None)
        )
        if moving:
            departure = self.pending_far or self.pending_fast or row
            self._end(departure.timestamp, "moved")
            self._start(departure)  # the vehicle now stands (or rolls) from here
            if departure is not row:
                self.feed(row)
            return
        self.pending_far = row if far else None
        self.pending_fast = row if fast else None
        if self.run.anchor is None and _trustworthy_fix(row):
            self.run.anchor = row
        if not far and not fast:
            self.run.last = row
            self.run.readings += 1
            self._maybe_confirm()

    def open_episode(self):
        """The CONFIRMED run still going at the end of the data, if any."""
        return self.run if self.run is not None and self.run.confirmed else None


# ---------------------------------------------------------------------------
# History window + reconciliation with stored IDLE alerts
# ---------------------------------------------------------------------------


def _history(vehicle, since):
    from apps.tracking.models import TelemetryEvent

    return (
        TelemetryEvent.objects.filter(vehicle=vehicle, timestamp__gte=since)
        .only("id", "timestamp", "ignition", "speed", "latitude", "longitude", "odometer", "satellite_count",
              "metadata", "device_id")
        .order_by("timestamp", "id")
    )


def _seed_from(alert):
    """Resume the CONFIRMED state of an idle alert that is still open, instead
    of replaying its whole (possibly hours-long) history: the alert row holds
    the run's start, anchor position, odometer and latest reading time."""
    from apps.tracking.models import TelemetryEvent

    def point(when, lat=0, lon=0):
        return TelemetryEvent(timestamp=when, latitude=lat, longitude=lon, ignition=True, odometer=alert.odometer)

    start = alert.telemetry_event or point(alert.occurred_at)
    anchor = point(alert.occurred_at, alert.latitude, alert.longitude) if alert.latitude is not None else None
    return IdleEpisode(start=start, last=point(alert.last_signal_at), anchor=anchor, origin_odometer=alert.odometer,
                       triggered_at=alert.triggered_at or alert.occurred_at)


def detect(vehicle, batch_start, batch_end, *, config=None):
    """Run the state machine over the history the batch [batch_start,
    batch_end] can affect. Returns (ended episodes, open confirmed episode)."""
    config = config or IdleConfig.for_vehicle(vehicle)
    open_alert = (
        Alert.objects.filter(category=Alert.Category.IDLE, vehicle=vehicle, signal_cleared_at__isnull=True)
        .select_related("telemetry_event").order_by("-occurred_at").first()
    )
    # >=: the comms bridge re-delivers the latest reading every pass; that must
    # not trigger a replay of the whole open episode.
    if open_alert is not None and open_alert.last_signal_at and batch_start >= open_alert.last_signal_at:
        machine = IdleMachine(config, seed=_seed_from(open_alert))
        since = open_alert.last_signal_at + datetime.timedelta(microseconds=1)
    else:
        machine = IdleMachine(config)
        since = batch_start - config.threshold - config.max_gap
        if open_alert is not None and open_alert.occurred_at < batch_start:
            since = min(since, open_alert.occurred_at)
    stop_after = batch_end + config.threshold + config.max_gap
    for row in _history(vehicle, since).iterator(chunk_size=500):
        if row.timestamp > stop_after and machine.state == IdleMachine.MOVING:
            break  # past everything this batch can influence
        machine.feed(row)
    return machine.episodes, machine.open_episode()


def _snapshot(episode):
    from apps.tracking.trip_report import _has_gps_fix

    start = episode.anchor or episode.start
    metadata = getattr(start, "metadata", None) or {}
    has_fix = _has_gps_fix(start.latitude, start.longitude)
    return {
        "occurred_at": episode.start.timestamp,
        "telemetry_event": episode.start if getattr(episode.start, "pk", None) else None,
        "latitude": start.latitude if has_fix else None,
        "longitude": start.longitude if has_fix else None,
        "location": (metadata.get("location") or "")[:255],
        "speed": episode.start.speed,
        "ignition": True,
        "odometer": episode.start.odometer,
    }


def _upsert(vehicle, episode, config, *, notify):
    """Merge one CONFIRMED episode into the IDLE alerts. Returns (alert, created)."""
    end = episode.end_at
    overlapping = (
        Alert.objects.filter(category=Alert.Category.IDLE, vehicle=vehicle)
        # Strict overlap: an episode that starts at the very reading where the
        # previous one ended (the vehicle moved, then stopped) is a new episode.
        .filter(Q(signal_cleared_at__isnull=True) | Q(signal_cleared_at__gt=episode.start.timestamp))
        .filter(occurred_at__lte=end or episode.last.timestamp)
        .order_by("occurred_at").first()
    )
    if overlapping is not None:
        fields = {
            "last_signal_at": max(filter(None, [overlapping.last_signal_at, episode.last.timestamp])),
            "signal_cleared_at": end,
        }
        if episode.start.timestamp < overlapping.occurred_at:
            fields.update(_snapshot(episode))  # late data: the idle began earlier than first seen
        changed = [name for name, value in fields.items() if getattr(overlapping, name) != value]
        for name in changed:
            setattr(overlapping, name, fields[name])
        if changed:
            overlapping.save(update_fields=[*changed, "updated_at"])
        return overlapping, False

    minutes = int(config.threshold.total_seconds() // 60)
    alert = Alert.objects.create(
        dedupe_key=f"IDLE:{vehicle.pk}:{episode.start.timestamp.isoformat()}",
        category=Alert.Category.IDLE,
        severity=Alert.Severity.MEDIUM,
        status=Alert.Status.OPEN,
        title=IDLE_TITLE.format(registration=vehicle.registration_number),
        message=f"Stationary with ignition ON for {minutes} min or more.",
        vehicle=vehicle,
        client_id=vehicle.client_id,
        driver_id=vehicle.current_driver_id,
        triggered_at=episode.triggered_at,
        last_signal_at=episode.last.timestamp,
        signal_cleared_at=end,
        **_snapshot(episode),
    )
    alert.link_url = f"{reverse('alerts:alert_report')}?alert={alert.uuid}"
    alert.save(update_fields=["link_url", "updated_at"])
    if notify:
        from apps.alerts.events import _is_recent, notify_alert

        if _is_recent(alert.triggered_at):
            notify_alert(alert)
    return alert, True


def process_vehicle_idle(*, vehicle, readings, notify=True):
    """Apply a batch of a vehicle's (already stored) readings to its idle
    state. Returns the IDLE alerts CREATED. Caller holds the vehicle lock and
    the transaction (apps.alerts.events.evaluate_ingested)."""
    if vehicle is None or not readings:
        return []
    config = IdleConfig.for_vehicle(vehicle)
    times = [r.timestamp for r in readings]
    ended, open_episode = detect(vehicle, min(times), max(times), config=config)
    created = []
    for episode in [*ended, *([open_episode] if open_episode else [])]:
        alert, is_new = _upsert(vehicle, episode, config, notify=notify)
        if is_new:
            created.append(alert)
    return created


def backfill(*, vehicles, since):
    """Replay stored history from ``since`` for each vehicle and record its
    idle episodes WITHOUT notifying anyone (they are past events). Idempotent:
    episodes already stored are matched by overlap. Returns alerts created."""
    from django.db import transaction

    from apps.alerts.events import _lock_vehicle

    created = 0
    for vehicle in vehicles:
        config = IdleConfig.for_vehicle(vehicle)
        with transaction.atomic():
            _lock_vehicle(vehicle.pk)
            machine = IdleMachine(config)
            for row in _history(vehicle, since).iterator(chunk_size=1000):
                machine.feed(row)
            open_episode = machine.open_episode()
            for episode in [*machine.episodes, *([open_episode] if open_episode else [])]:
                created += _upsert(vehicle, episode, config, notify=False)[1]
    return created
