"""Geofence monitoring: ENTRY / EXIT / SPEED_LIMIT events for ASSIGNED vehicles.

Called from the ingestion alert hook (apps.alerts.events.evaluate_ingested)
for every stored batch of a vehicle, under the same per-vehicle lock and
savepoint as the other telemetry alerts — so a geofence bug never loses
telemetry, and concurrent batches can't double-create anything.

Which geofences: only ACTIVE ones the vehicle is assigned to (one indexed
query per batch); each reading is pre-checked against the geofence's enclosing
circle before any polygon math, so cost grows with a vehicle's own assigned
geofences, never with the whole fleet x all geofences.

State per (vehicle, geofence)
-----------------------------
INSIDE / OUTSIDE = the latest GeofenceEvent (ENTER / EXIT). Readings are
judged in device-time order, re-run over a short window of the stored history
around each batch (late / out-of-order / re-delivered records give the same
result; crossings are matched by their time, so nothing is created twice):

  * only trustworthy GPS fixes count (no (0,0), no device "no fix", enough
    satellites — apps.tracking.trip_report._trustworthy_fix); a reading
    without one is not evaluated;
  * boundary hysteresis: OUTSIDE -> INSIDE when the point is inside the shape;
    INSIDE -> OUTSIDE only when it is more than GEOFENCE_EXIT_TOLERANCE_METERS
    outside it — GPS jitter on the edge can't flap;
  * a crossing must hold for GEOFENCE_CONFIRM_READINGS consecutive readings
    (default 2) — one GPS jump is not an entry; the event time is the first
    reading on the new side.

Alerts (apps.alerts.models.Alert, content_object = the geofence)
---------------------------------------------------------------
  ENTRY geofence        OUTSIDE -> INSIDE  -> one "Geofence Entry" (MEDIUM);
                        its end time is the next exit (time spent inside)
  EXIT geofence         INSIDE -> OUTSIDE  -> one "Geofence Exit" (MEDIUM);
                        its end time is the next entry (time spent outside)
  ENTRY_AND_EXIT        both of the above on the same geofence and state: an
                        Entry alert on each entry, an Exit alert on each exit
  SPEED_LIMIT geofence  only while INSIDE: speed > limit opens one "Geofence
                        Speeding" (HIGH); further fast readings only raise its
                        top speed; speed <= limit, or leaving the geofence,
                        clears it; a later speed > limit opens a new one.

Each new alert notifies through apps.alerts.events.notify_alert (bell, popup,
sound) exactly once.
"""

import dataclasses
import datetime
import math

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse

from apps.alerts.models import Alert
from apps.geofences.models import Geofence, GeofenceEvent

EARTH_RADIUS_METERS = 6371000
_WINDOW_BEFORE = datetime.timedelta(minutes=10)


def distance_meters(lat1, lon1, lat2, lon2):
    """Haversine distance — accurate enough for geofence comparisons."""
    phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2))
    d_phi = math.radians(float(lat2) - float(lat1))
    d_lambda = math.radians(float(lon2) - float(lon1))
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(a))


def _exit_tolerance():
    return float(getattr(settings, "GEOFENCE_EXIT_TOLERANCE_METERS", 20))


def _confirm_readings():
    return max(1, int(getattr(settings, "GEOFENCE_CONFIRM_READINGS", 2)))


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def _to_xy(lat, lon, lat0, lon0):
    """Local equirectangular projection in metres around (lat0, lon0)."""
    x = math.radians(lon - lon0) * EARTH_RADIUS_METERS * math.cos(math.radians(lat0))
    y = math.radians(lat - lat0) * EARTH_RADIUS_METERS
    return x, y


def _point_in_polygon(x, y, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _distance_to_ring(x, y, ring):
    best = math.inf
    for i in range(len(ring)):
        (x1, y1), (x2, y2) = ring[i], ring[(i + 1) % len(ring)]
        dx, dy = x2 - x1, y2 - y1
        t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
        best = min(best, math.hypot(x - (x1 + t * dx), y - (y1 + t * dy)))
    return best


def outside_by(geofence, lat, lon):
    """How far (m) the point is OUTSIDE the shape; <= 0 means inside."""
    lat, lon = float(lat), float(lon)
    to_center = distance_meters(lat, lon, geofence.center_latitude, geofence.center_longitude)
    if geofence.shape != Geofence.Shape.POLYGON or len(geofence.polygon or []) < 3:
        return to_center - geofence.radius_meters
    if to_center > geofence.radius_meters + 1000:
        return to_center - geofence.radius_meters  # far beyond the enclosing circle: no polygon math
    lat0, lon0 = float(geofence.center_latitude), float(geofence.center_longitude)
    ring = [_to_xy(p[0], p[1], lat0, lon0) for p in geofence.polygon]
    x, y = _to_xy(lat, lon, lat0, lon0)
    edge = _distance_to_ring(x, y, ring)
    return -edge if _point_in_polygon(x, y, ring) else edge


def is_inside(geofence, lat, lon, *, currently_inside=False):
    """Hysteresis: entering needs the point inside the shape; leaving needs it
    more than the exit tolerance outside."""
    distance_out = outside_by(geofence, lat, lon)
    return distance_out <= (_exit_tolerance() if currently_inside else 0)


# ---------------------------------------------------------------------------
# Per (vehicle, geofence) state machine over a window of readings
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _Crossing:
    kind: str  # GeofenceEvent.EventType
    reading: object


@dataclasses.dataclass
class _Speeding:
    start: object
    top_speed: object
    end_at: datetime.datetime = None


class _FenceMachine:
    def __init__(self, geofence, inside, open_speeding=None):
        self.geofence = geofence
        self.inside = inside
        self.pending = []  # readings on the other side, awaiting confirmation
        self.crossings = []
        self.speeding = []  # _Speeding episodes seen in the window (closed or open)
        self.current = open_speeding  # the open speeding episode, if any
        if open_speeding is not None:
            self.speeding.append(open_speeding)

    def feed(self, reading):
        side = is_inside(self.geofence, reading.latitude, reading.longitude, currently_inside=self.inside)
        if side == self.inside:
            self.pending = []
        else:
            self.pending.append(reading)
            if len(self.pending) >= _confirm_readings():
                confirming, self.pending = self.pending, []
                first = confirming[0]
                self.inside = side
                kind = GeofenceEvent.EventType.ENTER if side else GeofenceEvent.EventType.EXIT
                self.crossings.append(_Crossing(kind, first))
                if not side:
                    self._stop_speeding(first.timestamp)  # left the zone: stop monitoring its limit
                else:
                    for confirmed in confirming:  # entered: judge the confirming readings' speed, in order
                        self._judge_speed(confirmed)
                return
        if self.inside and not self.pending:
            self._judge_speed(reading)

    def _judge_speed(self, reading):
        limit = self.geofence.speed_limit_kmh
        if self.geofence.geofence_type != Geofence.GeofenceType.SPEED_LIMIT or not limit:
            return
        speed = reading.speed
        if speed is None:
            return
        if float(speed) > limit:
            if self.current is None:
                self.current = _Speeding(start=reading, top_speed=speed)
                self.speeding.append(self.current)
            elif speed > self.current.top_speed:
                self.current.top_speed = speed
        else:
            self._stop_speeding(reading.timestamp)

    def _stop_speeding(self, when):
        if self.current is not None:
            self.current.end_at = when
            self.current = None


# ---------------------------------------------------------------------------
# Persistence: GeofenceEvent log + alerts
# ---------------------------------------------------------------------------


def _geofence_ct():
    return ContentType.objects.get_for_model(Geofence)


def _alerts_for(geofence, vehicle, category):
    return Alert.objects.filter(category=category, vehicle=vehicle, content_type=_geofence_ct(),
                                object_id=geofence.pk)


def _create_alert(geofence, vehicle, category, severity, reading, message, *, speed_limit=None, notify):
    from apps.alerts.events import _is_recent, _snapshot, notify_alert

    t = reading.timestamp
    dedupe = f"{category}:{geofence.pk}:{vehicle.pk}:{t.isoformat()}"
    existing = Alert.objects.filter(dedupe_key=dedupe).first()
    if existing is not None:
        return existing, False
    fields = _snapshot(reading, reading if getattr(reading, "pk", None) else None, voltage_key="")
    fields.pop("voltage", None)
    alert = Alert.objects.create(
        dedupe_key=dedupe, category=category, severity=severity, status=Alert.Status.OPEN,
        title=f"{Alert.Category(category).label} alert — {vehicle.registration_number}",
        message=message[:500], vehicle=vehicle, client_id=vehicle.client_id, driver_id=vehicle.current_driver_id,
        content_type=_geofence_ct(), object_id=geofence.pk, triggered_at=t, last_signal_at=t,
        speed_limit=speed_limit, **fields,
    )
    alert.link_url = f"{reverse('alerts:alert_report')}?alert={alert.uuid}"
    alert.save(update_fields=["link_url", "updated_at"])
    if notify and _is_recent(t):
        notify_alert(alert)
    return alert, True


def _close_latest(geofence, vehicle, category, at):
    """End time of the latest open ENTRY/EXIT alert (time inside / outside)."""
    alert = (
        _alerts_for(geofence, vehicle, category).filter(signal_cleared_at__isnull=True, occurred_at__lt=at)
        .order_by("-occurred_at").first()
    )
    if alert is not None:
        alert.signal_cleared_at = alert.last_signal_at = at
        alert.save(update_fields=["signal_cleared_at", "last_signal_at", "updated_at"])


def _apply_crossing(geofence, vehicle, crossing, *, notify):
    r = crossing.reading
    if GeofenceEvent.objects.filter(geofence=geofence, vehicle=vehicle, event_type=crossing.kind,
                                    occurred_at=r.timestamp).exists():
        return []  # already recorded (re-delivered / replayed window)
    GeofenceEvent.objects.create(geofence=geofence, vehicle=vehicle, event_type=crossing.kind,
                                 latitude=r.latitude, longitude=r.longitude, occurred_at=r.timestamp)
    created = []
    entered = crossing.kind == GeofenceEvent.EventType.ENTER
    if entered:
        _close_latest(geofence, vehicle, Alert.Category.GEOFENCE_EXIT, r.timestamp)
    else:
        _close_latest(geofence, vehicle, Alert.Category.GEOFENCE_ENTRY, r.timestamp)
    reg = vehicle.registration_number
    if entered and geofence.alerts_on_entry:
        alert, is_new = _create_alert(geofence, vehicle, Alert.Category.GEOFENCE_ENTRY, Alert.Severity.MEDIUM, r,
                                      f"Vehicle {reg} entered {geofence.name}.", notify=notify)
        created += [alert] if is_new else []
    if not entered and geofence.alerts_on_exit:
        alert, is_new = _create_alert(geofence, vehicle, Alert.Category.GEOFENCE_EXIT, Alert.Severity.MEDIUM, r,
                                      f"Vehicle {reg} exited {geofence.name}.", notify=notify)
        created += [alert] if is_new else []
    return created


def _apply_speeding(geofence, vehicle, episode, *, notify):
    limit = geofence.speed_limit_kmh
    existing = getattr(episode, "alert", None)
    if existing is None:
        message = f"Speeding in {geofence.name}: {float(episode.start.speed):.0f} km/h (limit {limit} km/h)."
        existing, is_new = _create_alert(geofence, vehicle, Alert.Category.GEOFENCE_SPEEDING, Alert.Severity.HIGH,
                                         episode.start, message, speed_limit=limit, notify=notify)
    else:
        is_new = False
    fields = []
    if episode.top_speed is not None and (existing.speed is None or episode.top_speed > existing.speed):
        existing.speed = episode.top_speed
        fields.append("speed")
    if episode.end_at is not None and existing.signal_cleared_at != episode.end_at:
        existing.signal_cleared_at = existing.last_signal_at = episode.end_at
        fields += ["signal_cleared_at", "last_signal_at"]
    if fields:
        existing.save(update_fields=[*fields, "updated_at"])
    return [existing] if is_new else []


def _readings(vehicle, since):
    from apps.tracking.models import TelemetryEvent
    from apps.tracking.trip_report import _trustworthy_fix

    rows = (TelemetryEvent.objects.filter(vehicle=vehicle, timestamp__gte=since)
            .only("id", "timestamp", "latitude", "longitude", "speed", "ignition", "odometer", "satellite_count",
                  "metadata").order_by("timestamp", "id"))
    return [r for r in rows if _trustworthy_fix(r)]


def process_vehicle_geofences(*, vehicle, readings, notify=True):
    """Apply a batch (already stored) to every ACTIVE geofence the vehicle is
    assigned to. Returns the alerts CREATED. Caller holds lock + transaction."""
    if vehicle is None or not readings:
        return []
    geofences = list(Geofence.objects.filter(status=Geofence.Status.ACTIVE, vehicles=vehicle))
    if not geofences:
        return []
    window_start = min(r.timestamp for r in readings) - _WINDOW_BEFORE
    history = _readings(vehicle, window_start)
    if not history:
        return []
    created = []
    for geofence in geofences:
        before = (GeofenceEvent.objects.filter(geofence=geofence, vehicle=vehicle, occurred_at__lt=window_start)
                  .order_by("-occurred_at").values_list("event_type", flat=True).first())
        inside = before == GeofenceEvent.EventType.ENTER
        open_speeding = None
        if inside and geofence.geofence_type == Geofence.GeofenceType.SPEED_LIMIT:
            alert = (_alerts_for(geofence, vehicle, Alert.Category.GEOFENCE_SPEEDING)
                     .filter(signal_cleared_at__isnull=True, occurred_at__lt=window_start)
                     .order_by("-occurred_at").first())
            if alert is not None:
                open_speeding = _Speeding(start=None, top_speed=alert.speed)
                open_speeding.alert = alert
        machine = _FenceMachine(geofence, inside, open_speeding)
        for reading in history:
            machine.feed(reading)
        for crossing in machine.crossings:
            created += _apply_crossing(geofence, vehicle, crossing, notify=notify)
        for episode in machine.speeding:
            created += _apply_speeding(geofence, vehicle, episode, notify=notify)
    return created
