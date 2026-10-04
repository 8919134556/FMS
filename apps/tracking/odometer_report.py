"""Odometer Report: a daily, per-vehicle comparison of two INDEPENDENT distance
sources, computed from the existing ``TelemetryEvent`` history (nothing new is
stored — same approach as apps.tracking.trip_report):

    Device Odometer  ``TelemetryEvent.odometer`` — the vehicle/device's own
                     total-distance counter (comms ``odometer``, Teltonika total
                     odometer, stored in km; see apps.tracking.comms_sync). The
                     same field the Trip Report's distance and Live Tracking's
                     Odometer column use.
    GPS Odometer     a counter computed ONLY from latitude/longitude history:
                     the cumulative great-circle distance between consecutive
                     valid GPS fixes since the vehicle's first recorded fix —
                     the same rules as the Trip Report's GPS path length
                     (apps.tracking.trip_analytics: (0,0) no-fix readings
                     skipped, hops faster than MAX_PLAUSIBLE_SPEED_KMH ignored).

The two are never mixed: every row carries both, side by side.

Day boundaries are local days in the Settings timezone (as on the Trip Report).
Each day is computed separately:

    Device Start     the last reading BEFORE the day (the counter's value at
                     midnight — carried over, so travel across midnight is not
                     lost), else the day's first reading
    Device End       the day's last reading
    Device Distance  End − Start when the counter only rose; otherwise the sum
                     of the day's valid increments (see below), flagged
    GPS Start/End    the cumulative GPS distance before / at the end of the day
    GPS Distance     GPS End − GPS Start: the day's valid GPS hops

Data problems (all seen in real device data) are handled, never papered over:
    * odometer reset (a drop of more than RESET_THRESHOLD_KM, e.g. 295.4 → 0.0):
      a new counter segment starts; the day's distance adds the travel before
      and after the reset, and the row says so
    * small backward steps (rounding / out-of-order readings): ignored via a
      per-segment high-water mark, so they are not counted twice
    * spikes/jumps (a rise of more than JUMP_MIN_KM implying > MAX_PLAUSIBLE_SPEED_KMH):
      excluded
    * duplicate readings (same timestamp): contribute nothing
    * no readings / no odometer / no GPS fix on a day: that value is N/A
      (``None``), with a note — never 0 and never estimated

Performance: all reduction happens in PostgreSQL with window functions over
the existing ``(vehicle, timestamp)`` index (Django's ORM can't aggregate over
window results, hence parameterized raw SQL, as in trip_report). Only one
aggregated row per vehicle per day reaches Python — no GPS points are loaded
into Python or sent to the browser. The GPS Odometer's running total needs the
vehicle's full fix history up to the period end; that too is a single
server-side aggregate (see ``_gps_rows``).
"""

import dataclasses
import datetime

from django.db import connection
from django.utils import timezone

from apps.core.utils import display_timezone
from apps.tracking import trip_report
from apps.tracking.trip_analytics import MAX_PLAUSIBLE_SPEED_KMH

# A backward step larger than this is a counter reset; a smaller one is noise.
RESET_THRESHOLD_KM = 1.0
# A forward step is only a "jump" when it is BOTH implausibly fast AND larger
# than this: the counter reports in 0.1 km steps every few seconds, so a normal
# single step can momentarily "imply" hundreds of km/h and must still count.
JUMP_MIN_KM = 1.0
EARTH_RADIUS_KM = 6371.0088  # same constant as trip_analytics.haversine_km

def haversine_sql(lat1, lon1, lat2, lon2):
    """Great-circle distance in km between two SQL expressions — the SQL twin
    of trip_analytics.haversine_km (same formula, same EARTH_RADIUS_KM passed
    as the ``%(r)s`` query parameter). The one copy every report's SQL uses."""
    return (
        f"(2 * %(r)s * ASIN(LEAST(1, SQRT("
        f"POWER(SIN(RADIANS(({lat2}) - ({lat1})) / 2), 2) + "
        f"COS(RADIANS({lat1})) * COS(RADIANS({lat2})) * POWER(SIN(RADIANS(({lon2}) - ({lon1})) / 2), 2)))))"
    )


def implausible_sql(km, seconds):
    """True when covering ``km`` in ``seconds`` beats MAX_PLAUSIBLE_SPEED_KMH
    (``%(max_kmh)s``); a zero/negative interval with movement is implausible
    too. NULL-safe and never divides by zero."""
    return (f"COALESCE(({km}) > 0 AND (({seconds}) <= 0 OR ({km}) / NULLIF(({seconds}) / 3600.0, 0) > %(max_kmh)s), "
            f"FALSE)")


STATUS_OK = "ok"
STATUS_ISSUE = "issue"
STATUS_NO_DATA = "no_data"


def _tz_name(tz):
    return getattr(tz, "key", None) or str(tz)


# ---------------------------------------------------------------------------
# SQL — one aggregated row per (vehicle, local day)
# ---------------------------------------------------------------------------

_DEVICE_SQL = """
WITH prior AS (
    -- The last odometer reading before the period, per vehicle: the value
    -- carried into the first day (index scan on (vehicle, timestamp)).
    SELECT v.vid AS vehicle_id, p.id, p.ts, p.odometer
    FROM unnest(%(ids)s::bigint[]) AS v(vid)
    CROSS JOIN LATERAL (
        SELECT te.id, te."timestamp" AS ts, te.odometer
        FROM tracking_telemetryevent te
        WHERE te.vehicle_id = v.vid AND te.odometer IS NOT NULL AND te."timestamp" < %(start)s
        ORDER BY te."timestamp" DESC, te.id DESC
        LIMIT 1
    ) p
),
base AS (
    SELECT vehicle_id, id, "timestamp" AS ts, odometer::float8 AS odo
    FROM tracking_telemetryevent
    WHERE vehicle_id = ANY(%(ids)s) AND odometer IS NOT NULL
      AND "timestamp" >= %(start)s AND "timestamp" < %(end)s
    UNION ALL
    SELECT vehicle_id, id, ts, odometer::float8 FROM prior
),
raw AS (
    SELECT b.*,
        LAG(odo) OVER w AS p_odo, LAG(ts) OVER w AS p_ts,
        LEAD(odo) OVER w AS n_odo
    FROM base b
    WINDOW w AS (PARTITION BY vehicle_id ORDER BY ts, id)
),
marked AS (
    -- A spike: an implausibly fast rise that immediately falls back.
    SELECT *,
        COALESCE(p_odo IS NOT NULL AND odo > p_odo + %(jump_km)s
         AND (ts <= p_ts OR (odo - p_odo) / NULLIF(EXTRACT(EPOCH FROM ts - p_ts) / 3600.0, 0) > %(max_kmh)s)
         AND n_odo IS NOT NULL AND n_odo < odo, FALSE) AS is_spike
    FROM raw
),
clean AS (
    SELECT vehicle_id, id, ts, odo,
        LAG(odo) OVER w AS prev_odo, LAG(ts) OVER w AS prev_ts
    FROM marked WHERE NOT is_spike
    WINDOW w AS (PARTITION BY vehicle_id ORDER BY ts, id)
),
flagged AS (
    SELECT *,
        COALESCE(prev_odo IS NOT NULL AND odo < prev_odo - %(reset_km)s, FALSE) AS is_reset,
        COALESCE(prev_odo IS NOT NULL AND odo > prev_odo + %(jump_km)s
         AND (ts <= prev_ts OR (odo - prev_odo) / NULLIF(EXTRACT(EPOCH FROM ts - prev_ts) / 3600.0, 0) > %(max_kmh)s),
         FALSE) AS is_jump,
        COALESCE(prev_odo IS NOT NULL AND odo < prev_odo AND odo >= prev_odo - %(reset_km)s, FALSE) AS is_jitter
    FROM clean
),
segmented AS (
    SELECT *, SUM(is_reset::int) OVER (PARTITION BY vehicle_id ORDER BY ts, id) AS seg FROM flagged
),
watermarked AS (
    SELECT *, MAX(odo) OVER (PARTITION BY vehicle_id, seg ORDER BY ts, id
                             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS hw
    FROM segmented
),
steps AS (
    SELECT *,
        CASE
            WHEN prev_odo IS NULL OR is_reset OR is_jump THEN 0
            ELSE GREATEST(0, hw - COALESCE(LAG(hw) OVER (PARTITION BY vehicle_id, seg ORDER BY ts, id), hw))
        END AS inc
    FROM watermarked
)
SELECT vehicle_id, (ts AT TIME ZONE %(tz)s)::date AS day,
    COUNT(*) AS readings,
    (ARRAY_AGG(prev_odo ORDER BY ts, id))[1] AS carry_odo,
    (ARRAY_AGG(prev_ts ORDER BY ts, id))[1] AS carry_ts,
    (ARRAY_AGG(odo ORDER BY ts, id))[1] AS first_odo,
    (ARRAY_AGG(odo ORDER BY ts DESC, id DESC))[1] AS last_odo,
    SUM(inc) AS valid_km,
    COUNT(*) FILTER (WHERE is_reset) AS resets,
    (ARRAY_AGG(prev_odo ORDER BY ts, id) FILTER (WHERE is_reset))[1] AS reset_from,
    (ARRAY_AGG(odo ORDER BY ts, id) FILTER (WHERE is_reset))[1] AS reset_to,
    COUNT(*) FILTER (WHERE is_jump) AS jumps,
    COUNT(*) FILTER (WHERE is_jitter) AS jitters
FROM steps
WHERE ts >= %(start)s
GROUP BY vehicle_id, day
"""

# GPS: every valid fix up to the period end (the running total needs the
# whole history), single-point spikes removed, then great-circle hops summed
# per local day of the hop's END point; hops ending before the period collapse
# into one per-vehicle baseline row (day NULL).
_GPS_SQL = """
WITH fixes AS (
    SELECT vehicle_id, id, "timestamp" AS ts, latitude::float8 AS lat, longitude::float8 AS lon
    FROM tracking_telemetryevent
    WHERE vehicle_id = ANY(%(ids)s) AND "timestamp" < %(end)s
      AND NOT (latitude = 0 AND longitude = 0)
),
nb AS (
    SELECT f.*,
        LAG(lat) OVER w AS plat, LAG(lon) OVER w AS plon, LAG(ts) OVER w AS pts,
        LEAD(lat) OVER w AS nlat, LEAD(lon) OVER w AS nlon, LEAD(ts) OVER w AS nts
    FROM fixes f
    WINDOW w AS (PARTITION BY vehicle_id ORDER BY ts, id)
),
legs AS (
    SELECT nb.*,
        CASE WHEN plat IS NULL THEN NULL ELSE """ + haversine_sql("plat", "plon", "lat", "lon") + """ END AS in_km,
        CASE WHEN nlat IS NULL THEN NULL ELSE """ + haversine_sql("lat", "lon", "nlat", "nlon") + """ END AS out_km
    FROM nb
),
spikes AS (
    -- A fix is a spike when BOTH the hop into it and the hop out of it are
    -- implausible: dropping it lets the path continue from the last good fix,
    -- as trip_analytics.TripAnalysis does.
    SELECT vehicle_id, id, ts, lat, lon,
        COALESCE(in_km > 0 AND (ts <= pts OR in_km / NULLIF(EXTRACT(EPOCH FROM ts - pts) / 3600.0, 0) > %(max_kmh)s)
         AND out_km > 0 AND (nts <= ts OR out_km / NULLIF(EXTRACT(EPOCH FROM nts - ts) / 3600.0, 0) > %(max_kmh)s),
         FALSE) AS is_spike
    FROM legs
),
hops AS (
    SELECT vehicle_id, ts,
        EXTRACT(EPOCH FROM ts - LAG(ts) OVER w) AS secs,
        """ + haversine_sql("LAG(lat) OVER w", "LAG(lon) OVER w", "lat", "lon") + """ AS km
    FROM spikes WHERE NOT is_spike
    WINDOW w AS (PARTITION BY vehicle_id ORDER BY ts, id)
)
SELECT vehicle_id,
    CASE WHEN ts < %(start)s THEN NULL ELSE (ts AT TIME ZONE %(tz)s)::date END AS day,
    COALESCE(SUM(km) FILTER (WHERE km = 0 OR COALESCE(km / NULLIF(secs / 3600.0, 0) <= %(max_kmh)s, FALSE)), 0) AS valid_km,
    COUNT(*) FILTER (WHERE km > 0 AND NOT COALESCE(km / NULLIF(secs / 3600.0, 0) <= %(max_kmh)s, FALSE)) AS jumps
FROM hops
WHERE km IS NOT NULL
GROUP BY 1, 2
"""

_COUNTS_SQL = """
SELECT vehicle_id, ("timestamp" AT TIME ZONE %(tz)s)::date AS day,
    COUNT(*) AS readings,
    COUNT(*) FILTER (WHERE NOT (latitude = 0 AND longitude = 0)) AS fixes
FROM tracking_telemetryevent
WHERE vehicle_id = ANY(%(ids)s) AND "timestamp" >= %(start)s AND "timestamp" < %(end)s
GROUP BY vehicle_id, day
"""


def _fetch(sql, params):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        columns = [c.name for c in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class OdometerRow:
    date: datetime.date
    vehicle: object
    driver_name: str
    readings: int
    device_start: float | None
    device_end: float | None
    device_distance: float | None
    device_start_carried: bool  # start taken from the last reading before the day
    gps_start: float | None
    gps_end: float | None
    gps_distance: float | None
    notes: list
    status: str

    @property
    def difference_km(self):
        """Device − GPS, only when both distances exist."""
        if self.device_distance is None or self.gps_distance is None:
            return None
        return self.device_distance - self.gps_distance

    @property
    def difference_pct(self):
        diff = self.difference_km
        if diff is None or not self.gps_distance:
            return None
        return diff / self.gps_distance * 100


@dataclasses.dataclass
class OdometerReport:
    start_date: datetime.date
    end_date: datetime.date
    period_start: datetime.datetime
    period_end: datetime.datetime
    tz: datetime.tzinfo
    vehicles: list
    rows: list  # newest day first, then registration
    summary: dict
    vehicle_totals: list


def _round(value, places=3):
    return round(float(value), places) if value is not None else None


def build_odometer_report(*, user, start_date, end_date, vehicle_uuid="", search="", now=None):
    """Daily Device vs GPS odometer rows for every vehicle the caller can see
    (same scoping and filters as the Trip Report), ``start_date..end_date``
    inclusive (local dates)."""
    now = now or timezone.now()
    tz = display_timezone()
    vehicles = sorted(
        trip_report.scoped_tracked_vehicles(user, vehicle_uuid=vehicle_uuid, search=search),
        key=lambda v: v.registration_number,
    )
    period_start = datetime.datetime.combine(start_date, datetime.time.min, tzinfo=tz)
    period_end = min(datetime.datetime.combine(end_date + datetime.timedelta(days=1), datetime.time.min, tzinfo=tz), now)
    report = OdometerReport(start_date=start_date, end_date=end_date, period_start=period_start,
                            period_end=period_end, tz=tz, vehicles=vehicles, rows=[], summary={}, vehicle_totals=[])
    today = now.astimezone(tz).date()
    days = []
    day = end_date
    while day >= start_date:
        if day <= today:
            days.append(day)
        day -= datetime.timedelta(days=1)
    if not vehicles or not days:
        report.summary = _summarize(report)
        return report

    params = {
        "ids": [v.id for v in vehicles], "start": period_start, "end": period_end, "tz": _tz_name(tz),
        "max_kmh": float(MAX_PLAUSIBLE_SPEED_KMH), "reset_km": RESET_THRESHOLD_KM, "jump_km": JUMP_MIN_KM,
        "r": EARTH_RADIUS_KM,
    }
    device = {(r["vehicle_id"], r["day"]): r for r in _fetch(_DEVICE_SQL, params)}
    counts = {(r["vehicle_id"], r["day"]): r for r in _fetch(_COUNTS_SQL, params)}
    gps_days, gps_baseline, gps_jumps = {}, {}, {}
    for r in _fetch(_GPS_SQL, params):
        if r["day"] is None:
            gps_baseline[r["vehicle_id"]] = float(r["valid_km"])
        else:
            gps_days[(r["vehicle_id"], r["day"])] = float(r["valid_km"])
            gps_jumps[(r["vehicle_id"], r["day"])] = r["jumps"]

    resolve_driver = trip_report.driver_resolver(vehicles)
    for vehicle in vehicles:
        # GPS Odometer running total, walking the period's days oldest ->
        # newest from the distance accumulated before the period (0 when the
        # vehicle's history holds no earlier hop, i.e. the counter starts here).
        running = gps_baseline.get(vehicle.id, 0.0)
        gps_by_day = {}
        for day in reversed(days):
            count = counts.get((vehicle.id, day))
            if count and count["fixes"]:
                distance = gps_days.get((vehicle.id, day), 0.0)
                gps_by_day[day] = (running, running + distance, distance)
                running += distance
            else:
                # No valid fix that day: GPS distance unknown (N/A). A hop that
                # resumes the next day still counts — on the day it ends.
                gps_by_day[day] = (None, None, None)
        for day in days:
            report.rows.append(_row(vehicle, day, device.get((vehicle.id, day)), counts.get((vehicle.id, day)),
                                    gps_by_day[day], gps_jumps.get((vehicle.id, day), 0), resolve_driver))
    # Newest day first (as the Trip Report lists newest first), then registration.
    report.rows.sort(key=lambda r: (-r.date.toordinal(), r.vehicle.registration_number))
    report.summary = _summarize(report)
    report.vehicle_totals = _vehicle_totals(report)
    return report


def _row(vehicle, day, dev, count, gps, gps_jumps, resolve_driver):
    """Notes are ``{"level", "text"}``: a "warning" means a value is missing or
    had to be corrected (the row's status becomes "issue"); "info" explains a
    routine clean-up that doesn't make the numbers doubtful."""
    notes = []
    readings = count["readings"] if count else 0
    device_start = device_end = device_distance = None
    carried = False
    if dev:
        carried = dev["carry_odo"] is not None
        device_start = float(dev["carry_odo"] if carried else dev["first_odo"])
        device_end = float(dev["last_odo"])
        device_distance = float(dev["valid_km"] or 0)
        if dev["resets"]:
            notes.append(_note("warning",
                f"Device odometer reset ({float(dev['reset_from']):,.1f} → {float(dev['reset_to']):,.1f} km); "
                "distance adds travel before and after the reset"))
        if dev["jumps"]:
            notes.append(_note("warning", f"{dev['jumps']} implausible device odometer jump(s) excluded"))
        if dev["jitters"]:
            notes.append(_note("info", f"{dev['jitters']} small backward odometer step(s) ignored"))
    elif readings:
        notes.append(_note("warning", "No device odometer value in this day's readings"))

    gps_start, gps_end, gps_distance = gps
    if readings and gps_distance is None:
        notes.append(_note("warning", "No valid GPS fix on this day"))
    if gps_jumps:
        notes.append(_note("info", f"{gps_jumps} GPS jump(s) ignored"))
    if not readings:
        notes = [_note("info", "No data received")]
        device_start = device_end = device_distance = None
        gps_start = gps_end = gps_distance = None

    if not readings:
        status = STATUS_NO_DATA
    elif any(n["level"] == "warning" for n in notes):
        status = STATUS_ISSUE
    else:
        status = STATUS_OK
    driver = resolve_driver(vehicle, day)
    return OdometerRow(
        date=day, vehicle=vehicle, driver_name=driver.get_full_name() if driver else "", readings=readings,
        device_start=_round(device_start, 1), device_end=_round(device_end, 1),
        device_distance=_round(device_distance, 1), device_start_carried=carried,
        gps_start=_round(gps_start), gps_end=_round(gps_end), gps_distance=_round(gps_distance),
        notes=notes, status=status,
    )


def _note(level, text):
    return {"level": level, "text": text}


def _sum(values):
    values = [v for v in values if v is not None]
    return sum(values) if values else None


def _summarize(report):
    rows = report.rows
    device = _sum(r.device_distance for r in rows)
    gps = _sum(r.gps_distance for r in rows)
    comparable = [r for r in rows if r.difference_km is not None]
    device_cmp = _sum(r.device_distance for r in comparable)
    gps_cmp = _sum(r.gps_distance for r in comparable)
    return {
        "vehicles": len(report.vehicles),
        "vehicle_days": len(rows),
        "days_with_data": sum(1 for r in rows if r.status != STATUS_NO_DATA),
        "days_with_issues": sum(1 for r in rows if r.status == STATUS_ISSUE),
        "device_distance_km": _round(device, 1),
        "gps_distance_km": _round(gps, 3),
        # Difference over the vehicle-days where BOTH sources exist (like for like).
        "difference_km": _round(device_cmp - gps_cmp, 3) if comparable else None,
        "difference_pct": _round((device_cmp - gps_cmp) / gps_cmp * 100, 1) if comparable and gps_cmp else None,
    }


def _vehicle_totals(report):
    totals = []
    for vehicle in report.vehicles:
        rows = [r for r in report.rows if r.vehicle.id == vehicle.id]
        device = _sum(r.device_distance for r in rows)
        gps = _sum(r.gps_distance for r in rows)
        comparable = [r for r in rows if r.difference_km is not None]
        dc, gc = _sum(r.device_distance for r in comparable), _sum(r.gps_distance for r in comparable)
        totals.append({
            "vehicle": vehicle,
            "days_with_data": sum(1 for r in rows if r.status != STATUS_NO_DATA),
            "days_with_issues": sum(1 for r in rows if r.status == STATUS_ISSUE),
            "device_distance_km": _round(device, 1),
            "gps_distance_km": _round(gps, 3),
            "difference_km": _round(dc - gc, 3) if comparable else None,
            "difference_pct": _round((dc - gc) / gc * 100, 1) if comparable and gc else None,
        })
    return totals


def serialize_rows(rows):
    return [
        {
            "date": r.date.isoformat(),
            "vehicle_uuid": str(r.vehicle.uuid),
            "registration_number": r.vehicle.registration_number,
            "driver_name": r.driver_name or None,
            "readings": r.readings,
            "device_start_km": r.device_start,
            "device_end_km": r.device_end,
            "device_distance_km": r.device_distance,
            "device_start_carried": r.device_start_carried,
            "gps_start_km": r.gps_start,
            "gps_end_km": r.gps_end,
            "gps_distance_km": r.gps_distance,
            "difference_km": _round(r.difference_km, 3),
            "difference_pct": _round(r.difference_pct, 1),
            "notes": r.notes,
            "status": r.status,
        }
        for r in rows
    ]
