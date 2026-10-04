"""Trip Report: ignition-derived vehicle trips, computed on the fly from the
existing telemetry history (``TelemetryEvent`` — see apps.tracking.models).

Nothing new is stored. A "trip" here is not a database row; it is the result
of walking a vehicle's history in two stages.

1. CANDIDATES — the ignition ON/OFF rule (unchanged):

    ignition ON                  -> a CANDIDATE trip opens (not yet a trip)
    ignition OFF for <  closure  -> still the same candidate (a red light, a stop)
    ignition OFF for >= closure  -> the candidate ended the moment ignition went
                                    OFF; the next ignition ON opens a NEW one

2. CONFIRMATION — a candidate becomes a trip only once its readings show the
   vehicle genuinely moving (``_MovementValidator``); otherwise it is dropped
   (ignition on while parked, ON/OFF without driving, a single speed spike,
   GPS drift). Movement is confirmed by AGREEING, independent evidence, never
   one value (all thresholds per vehicle, Vehicle create/edit screen):

    A. sustained speed: at least ``trip_min_moving_records`` of the last
       ``trip_validation_records`` readings are at/above
       ``trip_min_speed_kmh``, AND the vehicle is at least
       ``trip_min_distance_m`` from where the ignition came on (by GPS or by
       odometer) — speed noise while parked can't create distance;
    B. corroborated displacement: the GPS position is at least
       ``trip_min_distance_m`` from where the ignition came on AND the device
       odometer advanced by that much too (without an odometer: the GPS
       displacement must hold on two consecutive readings) — for devices that
       report no speed. A single GPS spike can't move the odometer, and an
       odometer jump can't move the GPS, so neither alone confirms a trip;
    C. odometer distance: the device odometer advanced at least
       ``trip_min_distance_m`` AND at least ``ODOMETER_ONLY_MIN_KM`` (twice the
       0.1 km counter resolution, so rounding can't trigger it) at a plausible
       speed — a round trip with only ignition ON/OFF readings ends where it
       started, so GPS shows no displacement, but the wheels still turned. A
       counter reset/set (e.g. 70 -> 108,605 km in 30 s) is implausibly fast and
       never confirms.

   The trip START is where the vehicle actually departed — the first moving
   reading (A) or the last reading still at the parked spot (B, C) — not the
   ignition-ON reading, so parked/idle time before departure isn't counted.
   The trip END is unchanged: the ignition-OFF that the closure rule confirms.

``closure`` is a PER-VEHICLE business setting, ``Vehicle.trip_closure_minutes``
(Vehicle create/edit screen; default 5 — see
apps.vehicles.models.DEFAULT_TRIP_CLOSURE_MINUTES). Each vehicle's history is
reconstructed with its own value, read from that vehicle's row on every
request, so changing one vehicle's closure time re-splits that vehicle's
trips (past ones included) and never affects any other vehicle. This
is a different concept from ``apps.trips.Trip`` (the dispatch/logistics
model: a planned client shipment with an origin/destination Site, a driver
assignment, and a DRAFT->...->COMPLETED workflow) — that model is untouched.
This module answers a different question: "what did each vehicle actually
do, minute by minute, according to its own GPS device".

    TelemetryEvent (ignition, position, speed, odometer, per reading)
        -> compute_vehicle_trips() walks the ON/OFF transitions -> candidates
            -> _confirm_movement() reads each candidate's readings
                -> DetectedTrip (in memory only; never written back)
                    -> Trip Report page / API / exports (apps.tracking.api_views)

Performance: a naive "load every reading and walk it in Python" does not
scale (a fleet reporting every 10-30s can produce millions of rows over a
week). Instead ``_fetch_transitions`` asks PostgreSQL for only the rows
where ignition actually *changed* (a window function, ``LAG() OVER
(PARTITION BY vehicle ORDER BY timestamp)``), which Django's ORM cannot
filter on directly (window function results can't be referenced in a
WHERE/HAVING clause — Django raises "Filtering a query against a window
function is not permitted"), hence the one hand-written, parameterized SQL
query below — the same reasoning apps.tracking.comms_sync documents for its
own raw SQL. The reduction happens in the database on an indexed scan
(``TelemetryEvent`` already has a ``(vehicle, timestamp)`` index); only the
actual transition points cross into Python, typically a handful per vehicle
per day regardless of how often the device reports. Confirmation then reads
only each candidate's own readings — in ONE query for all candidates (a
LATERAL join), at most ``FIRST_PASS_READINGS`` each; a second query fetches
the rest only for the few candidates still undecided.
"""

import collections
import dataclasses
import datetime
import math
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from apps.core.scoping import scope_queryset
from apps.core.utils import display_timezone
from apps.tracking.models import TelemetryEvent, VehicleCurrentTelemetry
from apps.vehicles.models import Vehicle

# Date-range presets the Trip Report page offers (apps.tracking.views.TripReportView /
# apps.tracking.api_views.TripReportDataView) — "lastN" is N calendar days ending
# today, inclusive of today, which is the usual reading of "last 7 days".
RANGE_PRESETS = {"today": 1, "last3": 3, "last5": 5, "last7": 7}
# A custom range is capped so one request can't ask for years of history —
# LOOKBACK is added on top of whatever span is requested, so this bounds the
# total window PostgreSQL has to scan.
MAX_CUSTOM_RANGE_DAYS = 31

# How far before the requested range to look for ignition history, so a trip
# that started just before midnight (or before the range) is reconstructed
# from its real start instead of being clipped at the window edge. A vehicle
# whose ignition has been continuously ON/cycling for longer than this is a
# deliberate, documented boundary (see module docstring) — not expected in
# real fleet use, where "no stop as long as the vehicle's trip closure time
# for 2+ days" would itself be worth investigating. This is also why
# Vehicle.trip_closure_minutes is capped at one day (MAX_TRIP_CLOSURE_MINUTES).
LOOKBACK = datetime.timedelta(days=2)


@dataclasses.dataclass
class DetectedTrip:
    vehicle: Vehicle
    start_at: datetime.datetime
    end_at: datetime.datetime | None  # None while ACTIVE
    status: str  # "ACTIVE" | "COMPLETED"
    ignition: bool | None  # current ignition state (True while driving, False while OFF inside the closure window)
    start_latitude: Decimal | None
    start_longitude: Decimal | None
    start_location: str
    start_odometer: Decimal | None
    end_latitude: Decimal | None = None
    end_longitude: Decimal | None = None
    end_location: str = ""
    distance_km: Decimal | None = None

    def duration(self, *, now):
        """``now`` must be the same instant the caller used to decide ACTIVE
        vs COMPLETED (``compute_vehicle_trips``'s ``now``) — never a fresh
        ``timezone.now()`` read later, or a still-open trip's duration could
        outrun the moment its ACTIVE status was actually decided at."""
        return (self.end_at or now) - self.start_at


def _fetch_transitions(vehicle_ids, window_start, window_end):
    """Every row, per vehicle, where ``ignition`` differs from the previous
    reading for that vehicle (the first reading in the window always counts
    as a transition — ``IS DISTINCT FROM`` treats "no previous row" as
    different from any boolean, unlike ``=``, which would drop it).

    Deliberately NOT filtered by GPS fix quality (unlike ``trip_route()``):
    this query decides WHEN a trip starts/ends from ``ignition`` alone, which
    is independent of whether the device has a fix yet — excluding a no-fix
    row here could shift a trip's start/end time to whenever it next reports,
    not to when it actually happened. ``_finalize`` separately blanks out a
    boundary row's *position* (not its time) when that row has no fix."""
    if not vehicle_ids:
        return []
    sql = """
        SELECT id, vehicle_id, "timestamp", ignition, latitude, longitude, odometer, metadata
        FROM (
            SELECT
                te.id, te.vehicle_id, te."timestamp", te.ignition, te.latitude, te.longitude,
                te.odometer, te.metadata,
                LAG(te.ignition) OVER (PARTITION BY te.vehicle_id ORDER BY te."timestamp") AS prev_ignition
            FROM tracking_telemetryevent te
            WHERE te.vehicle_id = ANY(%s) AND te.ignition IS NOT NULL
              AND te."timestamp" >= %s AND te."timestamp" <= %s
        ) sub
        WHERE prev_ignition IS DISTINCT FROM ignition
        ORDER BY vehicle_id, "timestamp"
    """
    return list(TelemetryEvent.objects.raw(sql, [vehicle_ids, window_start, window_end]))


def _location_of(row):
    return (row.metadata or {}).get("location") or ""


def _has_gps_fix(latitude, longitude):
    """(0, 0) — "Null Island" — is the standard no-fix sentinel every GPS
    chipset reports before it has acquired satellites (seen directly in this
    fleet's real history: ``gps_status: 0`` readings recorded as lat=lon=0
    right as ignition turns on, before the device locks on). It is never a
    genuine vehicle location, so it must never be plotted as one — see the
    module docstring and ``trip_route()``."""
    return not (latitude == 0 and longitude == 0)


def _finalize(vehicle, start_row, *, status, end_row=None, pending_off=None):
    """``end_row`` confirms the trip COMPLETED at that timestamp. ``pending_off``
    means the trip is still ACTIVE but its last known reading was ignition OFF
    (inside the vehicle's closure window) — its position is shown as a tentative snapshot,
    normally overwritten a moment later by ``_apply_live_position`` with the
    vehicle's true current telemetry. Passing neither means "still driving"."""
    reference_off = end_row or pending_off
    start_has_fix = _has_gps_fix(start_row.latitude, start_row.longitude)
    trip = DetectedTrip(
        vehicle=vehicle,
        start_at=start_row.timestamp,
        end_at=end_row.timestamp if end_row else None,
        status=status,
        ignition=(status == "ACTIVE" and pending_off is None and end_row is None),
        start_latitude=start_row.latitude if start_has_fix else None,
        start_longitude=start_row.longitude if start_has_fix else None,
        start_location=_location_of(start_row) if start_has_fix else "",
        # Distance is the device's own odometer counter, unrelated to GPS fix
        # quality, so it stays keyed off the raw row even when position is None.
        start_odometer=start_row.odometer,
    )
    if reference_off is not None and _has_gps_fix(reference_off.latitude, reference_off.longitude):
        trip.end_latitude, trip.end_longitude = reference_off.latitude, reference_off.longitude
        trip.end_location = _location_of(reference_off)
    if reference_off is not None and (
        start_row.odometer is not None
        and reference_off.odometer is not None
        and reference_off.odometer >= start_row.odometer
    ):
        trip.distance_km = reference_off.odometer - start_row.odometer
    return trip


def trip_closure_window(vehicle):
    """How long ignition must stay OFF to close ``vehicle``'s current trip —
    its own ``trip_closure_minutes``. A missing/invalid value (not possible
    through the form or the NOT NULL column, but cheap to guard) falls back
    to the documented default rather than breaking the report."""
    from apps.vehicles.models import DEFAULT_TRIP_CLOSURE_MINUTES

    minutes = getattr(vehicle, "trip_closure_minutes", None)
    if not minutes or minutes < 1:
        minutes = DEFAULT_TRIP_CLOSURE_MINUTES
    return datetime.timedelta(minutes=minutes)


@dataclasses.dataclass
class _Candidate:
    """An ignition session the closure rule produced — a trip only once
    ``_confirm_movement`` finds genuine movement in its readings."""

    vehicle: Vehicle
    start_row: object  # the ignition-ON transition row
    status: str  # "ACTIVE" | "COMPLETED"
    end_row: object = None  # the closing ignition-OFF row (COMPLETED)
    pending_off: object = None  # an OFF still inside the closure window (ACTIVE)

    def until(self, now):
        return self.end_row.timestamp if self.end_row is not None else now


def _reconstruct(vehicle, rows, *, now, closure):
    """One vehicle's transition rows (ordered) -> its candidate sessions,
    using THIS vehicle's ``closure`` window (see ``trip_closure_window``).
    See the module docstring for the merge rule this implements — it is the
    same rule as before; only what happens next (confirmation) is new."""
    candidates = []
    start_row = None  # the ON row that opened the current candidate
    pending_off = None  # the OFF row that *might* close it, until proven by `closure`

    def close(off_row):
        candidates.append(_Candidate(vehicle, start_row, "COMPLETED", end_row=off_row))

    for row in rows:
        if row.ignition:  # an OFF -> ON transition (or the very first row, already ON)
            if start_row is None:
                start_row = row
            elif pending_off is not None:
                if row.timestamp - pending_off.timestamp >= closure:
                    close(pending_off)
                    start_row = row
                # else: back ON before the closure time — same candidate continues
                pending_off = None
        else:  # an ON -> OFF transition
            if start_row is not None:
                pending_off = row
            # else: vehicle was already off with no open candidate — nothing to end

    if start_row is not None:
        if pending_off is None:
            candidates.append(_Candidate(vehicle, start_row, "ACTIVE"))
        elif now - pending_off.timestamp >= closure:
            close(pending_off)
        else:
            # Still inside the closure window: not yet confirmed closed, so the
            # candidate stays ACTIVE (it may still resume) with no end time yet.
            candidates.append(_Candidate(vehicle, start_row, "ACTIVE", pending_off=pending_off))
    return candidates


# --------------------------------------------------------------------------
# Movement confirmation
# --------------------------------------------------------------------------

# Readings fetched per candidate in the first pass: real departures confirm
# within the first minutes even at 1-2 s reporting; only undecided candidates
# (long parked-with-ignition-on sessions) get a second pass for the rest.
FIRST_PASS_READINGS = 300
# A displacement implying more than this is a GPS jump, not departure — the
# same limit apps.tracking.trip_analytics uses for GPS distance.
MAX_PLAUSIBLE_SPEED_KMH = 250
# A fix from fewer satellites than this is too coarse to prove displacement.
MIN_SATELLITES = 4
# Rule C (odometer alone) needs at least this much: twice the 0.1 km
# resolution these devices report, so a single rounding step never counts.
ODOMETER_ONLY_MIN_KM = Decimal("0.2")


def _distance_m(a_lat, a_lon, b_lat, b_lon):
    lat1, lon1, lat2, lon2 = (math.radians(float(v)) for v in (a_lat, a_lon, b_lat, b_lon))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(min(1.0, math.sqrt(h)))


def _trustworthy_fix(row):
    """A position good enough to measure displacement with: not the (0,0)
    no-fix sentinel, not flagged no-fix by the device (``gps_status`` 0 keeps
    stale coordinates), and — when the device reports it — enough satellites."""
    if not _has_gps_fix(row.latitude, row.longitude):
        return False
    if str((row.metadata or {}).get("gps_status", "")) == "0":
        return False
    satellites = getattr(row, "satellite_count", None)
    return satellites is None or satellites >= MIN_SATELLITES


class _MovementValidator:
    """Fed one candidate's readings in time order; returns the DEPARTURE
    reading the moment genuine movement is confirmed (rules A/B in the module
    docstring), else None. States, for the record:

        VALIDATING  readings collected, evidence not yet sufficient
        CONFIRMED   feed() returned the departure row (caller stops feeding)
    """

    def __init__(self, vehicle):
        from apps.vehicles import models as vm

        def setting(name, default, low):
            value = getattr(vehicle, name, None)
            return value if isinstance(value, int) and value >= low else default

        self.window_size = setting("trip_validation_records", vm.DEFAULT_TRIP_VALIDATION_RECORDS, 2)
        self.required = min(setting("trip_min_moving_records", vm.DEFAULT_TRIP_MIN_MOVING_RECORDS, 1),
                            self.window_size)
        self.min_speed = setting("trip_min_speed_kmh", vm.DEFAULT_TRIP_MIN_SPEED_KMH, 1)
        self.min_distance = setting("trip_min_distance_m", vm.DEFAULT_TRIP_MIN_DISTANCE_M, 1)
        self.window = collections.deque(maxlen=self.window_size)  # (row, moving)
        self.origin_fix = None  # first trustworthy fix of the session: "where the ignition came on"
        self.origin_odometer = None
        self.at_origin = None  # latest reading still within min_distance of the origin (GPS)
        self.at_origin_odometer = None  # latest reading whose odometer hadn't advanced yet
        self.origin_odometer_row = None
        self.previous_far = False

    def _gps_far(self, row):
        if self.origin_fix is None or not _trustworthy_fix(row):
            return False
        metres = _distance_m(self.origin_fix.latitude, self.origin_fix.longitude, row.latitude, row.longitude)
        if metres < self.min_distance:
            return False
        hours = (row.timestamp - self.origin_fix.timestamp).total_seconds() / 3600
        # Implausibly fast = a GPS jump, not a departure.
        return hours > 0 and (metres / 1000) / hours <= MAX_PLAUSIBLE_SPEED_KMH

    def _odometer_advance_km(self, row):
        if self.origin_odometer is None or row.odometer is None:
            return None
        advanced = row.odometer - self.origin_odometer
        return advanced if advanced >= 0 else None  # a decrease = counter reset, not distance

    def _odometer_far(self, row):
        advanced = self._odometer_advance_km(row)
        return advanced is not None and float(advanced) * 1000 >= self.min_distance

    def _odometer_alone(self, row):
        """Rule C: a real, believable odometer advance on its own."""
        advanced = self._odometer_advance_km(row)
        if advanced is None or advanced < ODOMETER_ONLY_MIN_KM or float(advanced) * 1000 < self.min_distance:
            return False
        hours = (row.timestamp - self.origin_odometer_row.timestamp).total_seconds() / 3600
        return hours > 0 and float(advanced) / hours <= MAX_PLAUSIBLE_SPEED_KMH

    def feed(self, row):
        if self.origin_fix is None and _trustworthy_fix(row):
            self.origin_fix = row
        if self.origin_odometer is None and row.odometer is not None:
            self.origin_odometer = row.odometer
            self.origin_odometer_row = row
        speed = getattr(row, "speed", None)
        moving = speed is not None and row.ignition is not False and speed >= self.min_speed
        self.window.append((row, moving))

        gps_far = self._gps_far(row)
        odometer_far = self._odometer_far(row)
        if not gps_far and _trustworthy_fix(row):
            self.at_origin = row
        if not odometer_far and row.odometer is not None:
            self.at_origin_odometer = row

        # A — sustained speed, and the vehicle has actually left its spot
        # (with no position AND no odometer data at all, speed is all there is).
        no_distance_data = self.origin_fix is None and self.origin_odometer is None
        if sum(1 for _, m in self.window if m) >= self.required and (gps_far or odometer_far or no_distance_data):
            return next(r for r, m in self.window if m)

        # B — displacement corroborated by a second, independent signal.
        has_odometer = self.origin_odometer is not None and row.odometer is not None
        corroborated = odometer_far if has_odometer else self.previous_far
        if gps_far and corroborated:
            return self.at_origin or self.origin_fix

        # C — the wheels turned (odometer), even if the trip ended where it began.
        if self._odometer_alone(row):
            return self.at_origin_odometer or self.origin_odometer_row
        self.previous_far = gps_far
        return None


_CANDIDATE_READINGS_SQL = """
SELECT r.*, c.idx AS candidate_index
FROM unnest(%s::int[], %s::bigint[], %s::timestamptz[], %s::timestamptz[], %s::timestamptz[], %s::bigint[], %s::int[])
     AS c(idx, vid, st, en, after_ts, after_id, lim)
CROSS JOIN LATERAL (
    SELECT te.id, te.vehicle_id, te."timestamp", te.ignition, te.speed, te.latitude, te.longitude,
           te.odometer, te.satellite_count, te.metadata
    FROM tracking_telemetryevent te
    WHERE te.vehicle_id = c.vid AND te."timestamp" >= c.st AND te."timestamp" <= c.en
      AND (c.after_ts IS NULL OR (te."timestamp", te.id) > (c.after_ts, c.after_id))
    ORDER BY te."timestamp", te.id
    LIMIT c.lim
) r
ORDER BY c.idx, r."timestamp", r.id
"""


def _candidate_readings(batch, *, now, limit):
    """{candidate index: [readings…]} for ``batch`` = [(index, candidate, after_row)]
    — one round trip for every candidate (each uses the (vehicle, timestamp) index)."""
    if not batch:
        return {}
    columns = list(zip(*[
        (index, candidate.vehicle.id, candidate.start_row.timestamp, candidate.until(now),
         after.timestamp if after is not None else None, after.id if after is not None else None, limit)
        for index, candidate, after in batch
    ]))
    out = collections.defaultdict(list)
    for row in TelemetryEvent.objects.raw(_CANDIDATE_READINGS_SQL, [list(c) for c in columns]):
        out[row.candidate_index].append(row)
    return out


def _confirm_movement(candidates, *, now):
    """Candidates -> DetectedTrips, keeping only those with genuine movement;
    each trip starts at its departure reading (see the module docstring)."""
    validators = {i: _MovementValidator(c.vehicle) for i, c in enumerate(candidates)}
    departures = {}
    pending = [(i, c, None) for i, c in enumerate(candidates)]
    limit = FIRST_PASS_READINGS
    while pending:
        readings = _candidate_readings(pending, now=now, limit=limit)
        still_open = []
        for index, candidate, _after in pending:
            rows = readings.get(index, [])
            for row in rows:
                departure = validators[index].feed(row)
                if departure is not None:
                    departures[index] = departure
                    break
            else:
                if limit is not None and len(rows) == limit:
                    still_open.append((index, candidate, rows[-1]))  # more readings to look at
        pending, limit = still_open, None  # second pass: everything that's left
    return [
        _finalize(c.vehicle, departures[i], status=c.status, end_row=c.end_row, pending_off=c.pending_off)
        for i, c in enumerate(candidates) if i in departures
    ]


def _apply_live_position(active_trips):
    """ACTIVE trips show the vehicle's *current* position/odometer as their
    running "end" — the same VehicleCurrentTelemetry row the live map uses,
    refreshed on every request, so the trip visibly updates without ever
    being closed by a page reload."""
    if not active_trips:
        return
    current_by_vehicle = {
        c.vehicle_id: c
        for c in VehicleCurrentTelemetry.objects.filter(vehicle_id__in={t.vehicle.id for t in active_trips})
    }
    for trip in active_trips:
        current = current_by_vehicle.get(trip.vehicle.id)
        if current is None:
            continue
        trip.ignition = bool(current.ignition)
        # A momentary no-fix reading (see _has_gps_fix) must not blank out a
        # previously-known good position — keep showing the last real point
        # (the tentative pending_off snapshot, or the true start) until the
        # device locks on again.
        if _has_gps_fix(current.latitude, current.longitude):
            trip.end_latitude, trip.end_longitude = current.latitude, current.longitude
            trip.end_location = current.location
        if (
            trip.start_odometer is not None
            and current.odometer is not None
            and current.odometer >= trip.start_odometer
            # A current reading older than the trip's own start is stale/unrelated —
            # don't let it produce a nonsensical (or negative-looking) distance.
            and current.timestamp >= trip.start_at
        ):
            trip.distance_km = current.odometer - trip.start_odometer


def scoped_tracked_vehicles(user, *, vehicle_uuid="", search=""):
    """The vehicles a Trip Report request covers: every GPS-equipped vehicle
    ``user`` can see (apps.core.scoping), optionally narrowed to one vehicle
    and/or a registration/driver search — shared by the on-screen report and
    the downloadable exports (apps.tracking.trip_analytics) so both always
    cover exactly the same fleet."""
    vehicles_qs = scope_queryset(
        Vehicle.objects.select_related("current_driver", "vehicle_type").filter(tracking_device__isnull=False),
        user,
    )
    if vehicle_uuid:
        vehicles_qs = vehicles_qs.filter(uuid=vehicle_uuid)
    search = search.strip()
    if search:
        vehicles_qs = vehicles_qs.filter(
            Q(registration_number__icontains=search)
            | Q(current_driver__first_name__icontains=search)
            | Q(current_driver__last_name__icontains=search)
        )
    return list(vehicles_qs)


def trip_identifier(trip, *, tz):
    """A stable, human-readable ID for a detected trip. Trips aren't stored
    rows (see module docstring), but a trip's vehicle + start instant never
    change once detected, so ``<registration>-<local start YYYYMMDD-HHMMSS>``
    identifies it uniquely (one vehicle can't start two trips in the same
    second) and reads the same on screen, in the popup and in every export."""
    return f"{trip.vehicle.registration_number}-{trip.start_at.astimezone(tz):%Y%m%d-%H%M%S}"


def driver_resolver(vehicles):
    """``(vehicle, local_date) -> Driver | None`` — who was driving a vehicle
    on a given day.

    Prefers the dated assignment history (``VehicleDriverAssignment``) in
    force on that date — a trip from last week keeps last week's driver even
    if the vehicle has since been reassigned — and falls back to
    ``Vehicle.current_driver`` when no assignment covers the date. One query
    for all ``vehicles``, however many lookups follow."""
    from apps.vehicles.models import VehicleDriverAssignment

    assignments_by_vehicle = {}
    vehicle_ids = {v.id for v in vehicles}
    if vehicle_ids:
        for assignment in (
            VehicleDriverAssignment.objects.filter(vehicle_id__in=vehicle_ids)
            .exclude(status=VehicleDriverAssignment.Status.CANCELLED)
            .select_related("driver")
            # Primary before secondary/relief, then the most recent assignment first.
            .order_by("-primary_driver", "-start_date")
        ):
            assignments_by_vehicle.setdefault(assignment.vehicle_id, []).append(assignment)

    def resolve(vehicle, day):
        for assignment in assignments_by_vehicle.get(vehicle.id, ()):
            if assignment.start_date <= day and (assignment.end_date is None or day <= assignment.end_date):
                return assignment.driver
        return vehicle.current_driver

    return resolve


def resolve_trip_drivers(trips, *, tz):
    """``{id(trip): Driver | None}`` — the driver of each trip, per
    ``driver_resolver`` on the trip's local start date."""
    resolve = driver_resolver({t.vehicle for t in trips})
    return {id(t): resolve(t.vehicle, t.start_at.astimezone(tz).date()) for t in trips}


def compute_vehicle_trips(*, user, start_date, end_date, vehicle_uuid="", search="", now=None):
    """Trips for every vehicle the caller can see, bucketed by the local
    calendar date their trip started on, restricted to ``[start_date,
    end_date]`` (inclusive) — except a vehicle's currently ACTIVE trip is
    always included when ``end_date`` covers today, even if it actually
    started a day or two earlier (see the module docstring: an active trip
    is never hidden just because "today" was reloaded).

    Returns ``(trips, summary)``, trips sorted newest-first. ``user`` scopes
    to their client the same way every other tracking view does
    (apps.core.scoping) — only vehicles with a GPS device are considered
    (no device, no ignition data, nothing to detect).
    """
    now = now or timezone.now()
    tz = display_timezone()

    vehicles_by_id = {v.id: v for v in scoped_tracked_vehicles(user, vehicle_uuid=vehicle_uuid, search=search)}
    if not vehicles_by_id:
        return [], _summarize([], now=now)

    window_start = datetime.datetime.combine(start_date, datetime.time.min, tzinfo=tz) - LOOKBACK
    transitions_by_vehicle = {}
    for row in _fetch_transitions(list(vehicles_by_id), window_start, now):
        transitions_by_vehicle.setdefault(row.vehicle_id, []).append(row)

    candidates = []
    for vehicle_id, rows in transitions_by_vehicle.items():
        vehicle = vehicles_by_id[vehicle_id]
        candidates.extend(_reconstruct(vehicle, rows, now=now, closure=trip_closure_window(vehicle)))
    # Ignition ON is only a candidate: keep the ones the history shows moving.
    all_trips = _confirm_movement(candidates, now=now)

    _apply_live_position([t for t in all_trips if t.status == "ACTIVE"])

    today_local = now.astimezone(tz).date()
    include_active_regardless = end_date >= today_local
    visible = [
        t
        for t in all_trips
        if start_date <= t.start_at.astimezone(tz).date() <= end_date
        or (include_active_regardless and t.status == "ACTIVE")
    ]
    visible.sort(key=lambda t: t.start_at, reverse=True)

    return visible, _summarize(visible, now=now)


def resolve_date_range(range_key, from_str, to_str, *, now, tz):
    """``range_key`` -> ``(start_date, end_date)`` local dates, inclusive.
    Unrecognized/missing input safely falls back to "today" rather than
    erroring — a stale bookmark or a mistyped query param should never 500."""
    today_local = now.astimezone(tz).date()
    if range_key == "yesterday":
        yesterday = today_local - datetime.timedelta(days=1)
        return yesterday, yesterday
    if range_key in RANGE_PRESETS:
        return today_local - datetime.timedelta(days=RANGE_PRESETS[range_key] - 1), today_local
    if range_key == "custom" and from_str and to_str:
        try:
            start = datetime.date.fromisoformat(from_str)
            end = datetime.date.fromisoformat(to_str)
        except ValueError:
            return today_local, today_local
        if start > end:
            start, end = end, start
        max_span = datetime.timedelta(days=MAX_CUSTOM_RANGE_DAYS - 1)
        if end - start > max_span:
            start = end - max_span
        return start, end
    return today_local, today_local


def serialize_trips(trips, *, now):
    """DetectedTrip objects -> plain dicts for apps.tracking.serializers.TripReportSerializer."""
    tz = display_timezone()
    drivers = resolve_trip_drivers(trips, tz=tz)
    results = []
    for trip in trips:
        vehicle = trip.vehicle
        driver = drivers.get(id(trip))
        results.append(
            {
                "trip_id": trip_identifier(trip, tz=tz),
                "vehicle_uuid": vehicle.uuid,
                "registration_number": vehicle.registration_number,
                "vehicle_type": vehicle.vehicle_type.name if vehicle.vehicle_type_id else "",
                "driver_name": driver.get_full_name() if driver else None,
                "status": trip.status,
                "ignition": trip.ignition,
                "start_time": trip.start_at,
                "end_time": trip.end_at,
                "duration_seconds": int(trip.duration(now=now).total_seconds()),
                "start_location": trip.start_location,
                "end_location": trip.end_location,
                "start_latitude": trip.start_latitude,
                "start_longitude": trip.start_longitude,
                "end_latitude": trip.end_latitude,
                "end_longitude": trip.end_longitude,
                "distance_km": trip.distance_km,
            }
        )
    return results


# The Map column draws a lightweight route sparkline per row from real GPS
# points (see apps.tracking.api_views.TripRouteView / static/js/trip_report.js)
# rather than one live tile map per row — dozens of simultaneous Leaflet/OSM
# instances in one table would multiply tile requests far past what the OSM
# demo tile server documented elsewhere in this codebase (comms bridge,
# live_tracking.js) already warns is unfit for production load. A full,
# tiled map is used for the single trip the user actually opens.
MAX_ROUTE_POINTS = 300
# The trip popup's map (route + vehicle playback) asks for full resolution so
# the line — and the marker moving along it — follows every recorded fix
# instead of cutting corners between samples; very long trips are still
# evenly thinned beyond this.
MAX_DETAIL_ROUTE_POINTS = 5000


def trip_route(*, user, vehicle_uuid, start_at, end_at=None, now=None, max_points=MAX_ROUTE_POINTS):
    """The ordered, valid-fix GPS points recorded for one specific trip —
    reuses the same ``TelemetryEvent`` history the rest of this module reads,
    just unfiltered by ignition (every reading between the trip's start and
    end, not only the ON/OFF transitions ``compute_vehicle_trips`` needs),
    scoped strictly to this one vehicle and this one time window so points
    from another trip or another vehicle can never leak in.

    (0, 0) "no GPS fix yet" readings (see ``_has_gps_fix``) are excluded —
    this fleet's real history has them at the start of real trips, right as
    ignition turns on and before the device locks on; plotting them would
    draw a spurious line out to Null Island and collapse the real route into
    a sliver of the map.

    Returns ``None`` if the vehicle doesn't exist or isn't visible to
    ``user`` (apps.core.scoping) — the caller renders that as 404, never
    leaking whether a UUID belongs to someone else's fleet. ``end_at=None``
    (an in-progress trip) reads up to ``now``, matching how ACTIVE trips are
    open-ended everywhere else in this module — so an active trip's route
    keeps growing with the latest history on every call.
    """
    now = now or timezone.now()
    vehicle = (
        scope_queryset(Vehicle.objects.filter(tracking_device__isnull=False), user)
        .filter(uuid=vehicle_uuid)
        .first()
    )
    if vehicle is None:
        return None

    end_at = end_at or now
    points = list(
        TelemetryEvent.objects.filter(vehicle_id=vehicle.id, timestamp__gte=start_at, timestamp__lte=end_at)
        .exclude(latitude=0, longitude=0)
        .order_by("timestamp")
        .values_list("timestamp", "latitude", "longitude")
    )
    max_points = max(2, min(int(max_points), MAX_DETAIL_ROUTE_POINTS))
    thinned = _downsample(points, max_points)
    return {
        "vehicle_uuid": vehicle.uuid,
        "start_time": start_at,
        "end_time": end_at,
        "truncated": len(thinned) < len(points),
        "points": [{"t": t, "lat": lat, "lon": lon} for t, lat, lon in thinned],
    }


def _downsample(points, max_points):
    """Evenly-spaced sample of ``points``, always keeping the first and last
    (the trip's true start/end) — a route of a few hundred pings renders a
    sparkline just as well as one of several thousand, at a fraction of the
    payload and draw cost."""
    if len(points) <= max_points:
        return points
    if max_points <= 1:
        return points[:1]
    step = (len(points) - 1) / (max_points - 1)
    indices = sorted({round(i * step) for i in range(max_points)})
    return [points[i] for i in indices]


def _summarize(trips, *, now):
    distances = [t.distance_km for t in trips if t.distance_km is not None]
    durations = [t.duration(now=now) for t in trips]
    return {
        "total_trips": len(trips),
        "active_trips": sum(1 for t in trips if t.status == "ACTIVE"),
        "total_distance_km": sum(distances) if distances else None,
        "avg_duration_seconds": int(sum(d.total_seconds() for d in durations) / len(durations)) if durations else None,
    }
