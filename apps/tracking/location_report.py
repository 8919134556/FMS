"""Location Data Report: EVERY GPS/location record of the history table
(``TelemetryEvent``) for the chosen vehicle(s) and period — not trip
summaries — plus an analysis of that data and a per-record data-quality flag.

Sources (no new tables, nothing stored):

    records     tracking_telemetryevent, each column exactly as stored:
                timestamp, latitude, longitude, speed, heading, altitude,
                ignition, odometer, engine_hours, battery_voltage,
                external_power, signal_strength, satellite_count, and from
                ``metadata``: location (address), gps_status, gps_odometer_km.
                Columns that are empty for the whole selection are reported
                as such (``column_counts``) so the UI/exports can hide them.
    analysis    trip_analytics.TripAnalysis — the Trip Report's own engine —
                fed every record of each vehicle in time order: GPS distance
                (jumps ignored), moving / idle / engine-off / no-data time,
                stops, max speed. Nothing is re-implemented here.
    flags       computed in PostgreSQL next to each record, so paging and
                sorting stay server-side; thresholds are the ones the other
                reports use (trip_analytics.MAX_SAMPLE_GAP, MAX_PLAUSIBLE_SPEED_KMH,
                the (0,0) no-fix rule of trip_report._has_gps_fix).

Records are never dropped or altered: a problematic record is listed with its
flag(s). "Missing timestamp" can't occur — ``timestamp`` is NOT NULL.
"""

import contextlib
import dataclasses
import datetime

from django.db import connection, transaction
from django.utils import timezone

from apps.core.utils import display_timezone
from apps.tracking import trip_analytics, trip_report
from apps.tracking.odometer_report import EARTH_RADIUS_KM, _tz_name, haversine_sql, implausible_sql
from apps.tracking.trip_analytics import MAX_PLAUSIBLE_SPEED_KMH, MAX_SAMPLE_GAP, ReportError

PAGE_SIZES = (100, 250, 500)
DEFAULT_PAGE_SIZE = 100
# Exports carry every record. Excel's hard limit is ~1.05M rows per sheet; a
# PDF beyond a few thousand pages is not a document anyone can use, so it
# is refused (never silently truncated) with a pointer to the Excel file.
EXCEL_MAX_ROWS = 1_000_000
PDF_MAX_ROWS = 20_000

FLAG_LABELS = {
    "no_fix": "No GPS fix (0,0)",
    "device_no_fix": "Device reports no GPS fix",
    "invalid_coords": "Invalid coordinates",
    "duplicate": "Duplicate timestamp",
    "gap": "GPS gap before",
    "jump": "GPS jump",
    "invalid_speed": "Invalid speed",
}

# Optional columns: shown only when the selection has data in them.
OPTIONAL_COLUMNS = (
    "heading", "altitude", "engine_hours", "battery_voltage", "external_power",
    "signal_strength", "satellite_count", "gps_status", "gps_odometer_km", "location",
)

# Server-side sort keys (whitelist -> SQL); ties always fall back to time.
SORTS = {
    "time": "ts", "speed": "speed", "odometer": "odometer", "ignition": "ignition",
    "latitude": "lat", "longitude": "lon", "heading": "heading", "altitude": "altitude",
    "satellites": "satellite_count", "battery": "battery_voltage", "signal": "signal_strength",
    "engine_hours": "engine_hours", "vehicle": "registration_number", "status": "flag_count",
}

_FLAGGED_CTE = """
WITH base AS (
    SELECT te.id, te.vehicle_id, v.uuid AS vehicle_uuid, v.registration_number, te."timestamp" AS ts,
        te.latitude::float8 AS lat, te.longitude::float8 AS lon, te.speed::float8 AS speed, te.heading,
        te.altitude::float8 AS altitude, te.ignition, te.odometer::float8 AS odometer,
        te.engine_hours::float8 AS engine_hours, te.battery_voltage::float8 AS battery_voltage,
        te.external_power, te.signal_strength, te.satellite_count,
        te.metadata ->> 'location' AS location, te.metadata ->> 'gps_status' AS gps_status,
        te.metadata ->> 'gps_odometer_km' AS gps_odometer_km,
        (te.latitude = 0 AND te.longitude = 0) AS no_fix,
        (NOT (te.latitude = 0 AND te.longitude = 0)
         AND (te.latitude NOT BETWEEN -90 AND 90 OR te.longitude NOT BETWEEN -180 AND 180)) AS invalid_coords
    FROM tracking_telemetryevent te
    JOIN vehicles_vehicle v ON v.id = te.vehicle_id
    WHERE te.vehicle_id = ANY(%(ids)s) AND te."timestamp" >= %(start)s AND te."timestamp" < %(end)s
),
seq AS (
    SELECT b.*, LAG(ts) OVER (PARTITION BY vehicle_id ORDER BY ts, id) AS prev_ts FROM base b
),
fixes AS (
    SELECT id, vehicle_id, ts, lat, lon,
        LAG(lat) OVER w AS plat, LAG(lon) OVER w AS plon, LAG(ts) OVER w AS pts,
        LEAD(lat) OVER w AS nlat, LEAD(lon) OVER w AS nlon, LEAD(ts) OVER w AS nts
    FROM base WHERE NOT no_fix AND NOT invalid_coords
    WINDOW w AS (PARTITION BY vehicle_id ORDER BY ts, id)
),
spikes AS (
    -- A single-fix spike: both the hop into it and the hop out of it are impossible.
    SELECT id, vehicle_id, ts, lat, lon,
        CASE WHEN plat IS NULL OR nlat IS NULL THEN FALSE ELSE
            __IN_IMPLAUSIBLE__ AND __OUT_IMPLAUSIBLE__ END AS is_spike
    FROM fixes
),
clean AS (
    -- Other impossible hops, measured from the previous non-spike fix.
    SELECT id, LAG(lat) OVER w AS plat, LAG(lon) OVER w AS plon, LAG(ts) OVER w AS pts, lat, lon, ts
    FROM spikes WHERE NOT is_spike
    WINDOW w AS (PARTITION BY vehicle_id ORDER BY ts, id)
),
jumps AS (
    SELECT id FROM spikes WHERE is_spike
    UNION
    SELECT id FROM clean WHERE plat IS NOT NULL AND __HOP_IMPLAUSIBLE__
),
flagged AS (
    SELECT seq.*,
        EXTRACT(EPOCH FROM ts - prev_ts) AS gap_seconds,
        COALESCE(gps_status = '0' AND NOT no_fix, FALSE) AS device_no_fix,
        COALESCE(prev_ts = ts, FALSE) AS duplicate,
        COALESCE(EXTRACT(EPOCH FROM ts - prev_ts) > %(gap_seconds)s, FALSE) AS gap,
        -- A same-instant record is already a "duplicate"; its zero-second hop is
        -- not also counted as a jump (one problem, one flag).
        (jumps.id IS NOT NULL AND NOT COALESCE(prev_ts = ts, FALSE)) AS jump,
        COALESCE(speed < 0 OR speed > %(max_kmh)s, FALSE) AS invalid_speed
    FROM seq LEFT JOIN jumps ON jumps.id = seq.id
),
final AS (
    SELECT flagged.*,
        (no_fix::int + device_no_fix::int + invalid_coords::int + duplicate::int + gap::int
         + jump::int + invalid_speed::int) AS flag_count
    FROM flagged
)
"""
_FLAGGED_CTE = (
    _FLAGGED_CTE
    .replace("__IN_IMPLAUSIBLE__", implausible_sql(haversine_sql("plat", "plon", "lat", "lon"),
                                                   "EXTRACT(EPOCH FROM ts - pts)"))
    .replace("__OUT_IMPLAUSIBLE__", implausible_sql(haversine_sql("lat", "lon", "nlat", "nlon"),
                                                    "EXTRACT(EPOCH FROM nts - ts)"))
    .replace("__HOP_IMPLAUSIBLE__", implausible_sql(haversine_sql("plat", "plon", "lat", "lon"),
                                                    "EXTRACT(EPOCH FROM ts - pts)"))
)

_RECORD_COLUMNS = (
    "id, vehicle_uuid, registration_number, ts, lat, lon, speed, heading, altitude, ignition, odometer, "
    "engine_hours, battery_voltage, external_power, signal_strength, satellite_count, location, gps_status, "
    "gps_odometer_km, gap_seconds, no_fix, device_no_fix, invalid_coords, duplicate, gap, jump, invalid_speed, "
    "flag_count"
)

_QUALITY_SQL = _FLAGGED_CTE + """
SELECT COUNT(*) AS records,
    COUNT(*) FILTER (WHERE NOT no_fix AND NOT invalid_coords) AS valid_fixes,
    COUNT(*) FILTER (WHERE no_fix) AS no_fix,
    COUNT(*) FILTER (WHERE device_no_fix) AS device_no_fix,
    COUNT(*) FILTER (WHERE invalid_coords) AS invalid_coords,
    COUNT(*) FILTER (WHERE duplicate) AS duplicate,
    COUNT(*) FILTER (WHERE gap) AS gap,
    MAX(gap_seconds) FILTER (WHERE gap) AS longest_gap_seconds,
    COUNT(*) FILTER (WHERE jump) AS jump,
    COUNT(*) FILTER (WHERE invalid_speed) AS invalid_speed,
    COUNT(*) FILTER (WHERE flag_count > 0) AS flagged,
    MIN(ts) AS first_ts, MAX(ts) AS last_ts,
    COUNT(DISTINCT vehicle_id) AS vehicles_with_data,
    COUNT(heading) AS heading, COUNT(altitude) AS altitude, COUNT(engine_hours) AS engine_hours,
    COUNT(battery_voltage) AS battery_voltage, COUNT(external_power) AS external_power,
    COUNT(signal_strength) AS signal_strength, COUNT(satellite_count) AS satellite_count,
    COUNT(gps_status) AS gps_status, COUNT(gps_odometer_km) AS gps_odometer_km,
    COUNT(NULLIF(location, '')) AS location
FROM final
"""

QUALITY_FILTERS = {"": "", "valid": "WHERE flag_count = 0", "issues": "WHERE flag_count > 0"}


# ---------------------------------------------------------------------------
# Scope + period
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Selection:
    vehicles: list
    all_vehicles: bool
    range_key: str
    start_date: datetime.date
    end_date: datetime.date
    period_start: datetime.datetime
    period_end: datetime.datetime
    tz: datetime.tzinfo

    def params(self):
        return {
            "ids": [v.id for v in self.vehicles], "start": self.period_start, "end": self.period_end,
            "tz": _tz_name(self.tz), "max_kmh": float(MAX_PLAUSIBLE_SPEED_KMH), "r": EARTH_RADIUS_KM,
            "gap_seconds": MAX_SAMPLE_GAP.total_seconds(),
        }


def resolve_selection(*, user, vehicle, range_key, from_str="", to_str="", now=None):
    """``vehicle`` is a vehicle uuid or "all" (every vehicle the user can see).
    Strict validation, like the report downloads: bad input is an error with a
    readable message, never a silently different selection."""
    now = now or timezone.now()
    tz = display_timezone()
    vehicle = (vehicle or "").strip()
    if not vehicle:
        raise ReportError("Select a vehicle (or All vehicles) first.")
    range_key, start_date, end_date = trip_analytics.validate_export_range(range_key, from_str, to_str, now=now, tz=tz)
    all_vehicles = vehicle.lower() == "all"
    vehicles = trip_report.scoped_tracked_vehicles(user, vehicle_uuid="" if all_vehicles else vehicle)
    if not vehicles:
        raise ReportError(
            "No GPS-tracked vehicles are available to you." if all_vehicles
            else "The selected vehicle was not found or you don't have access to it.", status=404)
    period_start = datetime.datetime.combine(start_date, datetime.time.min, tzinfo=tz)
    period_end = min(datetime.datetime.combine(end_date + datetime.timedelta(days=1), datetime.time.min, tzinfo=tz), now)
    return Selection(sorted(vehicles, key=lambda v: v.registration_number), all_vehicles, range_key,
                     start_date, end_date, period_start, period_end, tz)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def consistent_snapshot():
    """One report = one view of the data. The comms bridge imports readings
    with a delay, so rows with timestamps INSIDE the period can arrive between
    the summary query and the record stream; under PostgreSQL's default READ
    COMMITTED each statement would see a different set (e.g. a summary of 4,087
    records over a sheet of 4,089). REPEATABLE READ pins every query of the
    request to one snapshot.

    PostgreSQL only accepts SET TRANSACTION as a transaction's FIRST statement,
    so the level is raised only when this opens the outermost transaction. If a
    caller already holds one (e.g. a test case, or code wrapped in atomic()),
    that transaction's own isolation governs and is left untouched."""
    if connection.in_atomic_block:
        yield
        return
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        yield


def _fetch(sql, params):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        columns = [c.name for c in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def quality_summary(selection):
    return _fetch(_QUALITY_SQL, selection.params())[0]


def _records_sql(sort, direction, quality, *, paged):
    column = SORTS.get(sort, "ts")
    order = "DESC" if direction == "desc" else "ASC"
    tail = " LIMIT %(limit)s OFFSET %(offset)s" if paged else ""
    return (_FLAGGED_CTE + f"SELECT {_RECORD_COLUMNS} FROM final {QUALITY_FILTERS.get(quality, '')} "
            f"ORDER BY {column} {order} NULLS LAST, ts ASC, id ASC{tail}")


def records_page(selection, *, page=1, page_size=DEFAULT_PAGE_SIZE, sort="time", direction="asc", quality=""):
    page_size = page_size if page_size in PAGE_SIZES else DEFAULT_PAGE_SIZE
    params = selection.params()
    count_sql = _FLAGGED_CTE + f"SELECT COUNT(*) AS n FROM final {QUALITY_FILTERS.get(quality, '')}"
    total = _fetch(count_sql, params)[0]["n"]
    pages = max(1, -(-total // page_size))
    page = min(max(1, page), pages)
    rows = _fetch(_records_sql(sort, direction, quality, paged=True),
                  {**params, "limit": page_size, "offset": (page - 1) * page_size})
    return {"total": total, "page": page, "pages": pages, "page_size": page_size, "rows": rows}


def iter_records(selection, *, quality=""):
    """Every matching record, oldest first, streamed with a server-side cursor
    (exports never hold the whole period in memory at once)."""
    with connection.chunked_cursor() as cursor:
        cursor.execute(_records_sql("time", "asc", quality, paged=False), selection.params())
        columns = [c.name for c in cursor.description]
        while True:
            chunk = cursor.fetchmany(2000)
            if not chunk:
                break
            for row in chunk:
                yield dict(zip(columns, row))


def record_flags(row):
    flags = [key for key in FLAG_LABELS if row.get(key)]
    return [FLAG_LABELS[key] + (f" ({int(row['gap_seconds'] // 60)} min)" if key == "gap" else "") for key in flags]


# ---------------------------------------------------------------------------
# Analysis (the Trip Report's engine over the whole period)
# ---------------------------------------------------------------------------


def analyse(selection, quality, *, now=None):
    now = now or timezone.now()
    total = quality["records"]
    if total > trip_analytics.EXPORT_MAX_GPS_RECORDS:
        raise ReportError(
            f"This selection contains {total:,} GPS records — too many to analyse in one request. "
            "Select a single vehicle or a shorter date range.", status=413)
    vehicles_by_id = {v.id: v for v in selection.vehicles}
    per_vehicle = {}
    ignition = {}
    for point in trip_analytics.stream_points(list(vehicles_by_id), selection.period_start, selection.period_end):
        entry = per_vehicle.get(point.vehicle_id)
        if entry is None:
            # A "trip" spanning the vehicle's records in the period, so TripAnalysis
            # measures GPS distance/durations over all of them (no odometer mixing).
            span = trip_report.DetectedTrip(
                vehicle=vehicles_by_id[point.vehicle_id], start_at=point.timestamp, end_at=point.timestamp,
                status="COMPLETED", ignition=None, start_latitude=None, start_longitude=None,
                start_location="", start_odometer=None,
            )
            entry = per_vehicle[point.vehicle_id] = trip_analytics.TripAnalysis(span, now=now)
            ignition[point.vehicle_id] = {"prev": None, "on": 0, "off": 0}
        entry.trip.end_at = point.timestamp
        entry.add(point)
        state = ignition[point.vehicle_id]
        if point.ignition is not None:
            if state["prev"] is False and point.ignition is True:
                state["on"] += 1
            elif state["prev"] is True and point.ignition is False:
                state["off"] += 1
            state["prev"] = point.ignition
    analyses = [a.finish() for a in per_vehicle.values()]
    if not analyses:
        return None

    def total_of(attr):
        return sum(getattr(a, attr) for a in analyses)

    top = max(analyses, key=lambda a: a.max_speed_kmh if a.max_speed_kmh is not None else -1)
    trusted = [a for a in analyses if a.avg_moving_speed_kmh is not None]
    moving_for_avg = sum(a.moving_seconds for a in trusted)
    longest = max((s for a in analyses for s in a.stops), key=lambda s: s.duration_seconds, default=None)
    gps_km = sum(a.gps_distance_km for a in analyses if a.fix_count >= 2)
    return {
        "gps_distance_km": round(gps_km, 3) if any(a.fix_count >= 2 for a in analyses) else None,
        "first_record": min(a.first_point.timestamp for a in analyses),
        "last_record": max(a.last_point.timestamp for a in analyses),
        "tracking_seconds": total_of("duration_seconds"),
        "moving_seconds": total_of("moving_seconds"),
        "idle_seconds": total_of("idle_seconds"),
        "engine_off_seconds": total_of("engine_off_seconds"),
        "stopped_seconds": total_of("idle_seconds") + total_of("engine_off_seconds"),
        "no_data_seconds": total_of("no_data_seconds"),
        "ignition_on_seconds": total_of("moving_seconds") + total_of("idle_seconds"),
        "ignition_off_seconds": total_of("engine_off_seconds"),
        "ignition_on_events": sum(s["on"] for s in ignition.values()),
        "ignition_off_events": sum(s["off"] for s in ignition.values()),
        "max_speed_kmh": top.max_speed_kmh,
        "max_speed_at": top.max_speed_point.timestamp if top.max_speed_point else None,
        "max_speed_vehicle": top.trip.vehicle.registration_number if top.max_speed_point else None,
        "avg_moving_speed_kmh": (
            round(sum(a.gps_distance_km for a in trusted) / (moving_for_avg / 3600), 1) if moving_for_avg else None
        ),
        "stops": sum(len(a.stops) for a in analyses),
        "longest_stop_seconds": longest.duration_seconds if longest else None,
        "moving_threshold_kmh": int(trip_analytics.moving_threshold_kmh()),
        "gap_threshold_minutes": int(MAX_SAMPLE_GAP.total_seconds() // 60),
    }


def serialize_record(row):
    def f(value, places=None):
        if value is None:
            return None
        return round(float(value), places) if places is not None else value

    return {
        "id": row["id"],
        "timestamp": row["ts"].isoformat(),
        "vehicle_uuid": str(row["vehicle_uuid"]),
        "registration_number": row["registration_number"],
        "latitude": f(row["lat"], 6), "longitude": f(row["lon"], 6),
        "speed": f(row["speed"], 2), "heading": row["heading"], "altitude": f(row["altitude"], 2),
        "ignition": row["ignition"], "odometer": f(row["odometer"], 1), "engine_hours": f(row["engine_hours"], 1),
        "battery_voltage": f(row["battery_voltage"], 2), "external_power": row["external_power"],
        "signal_strength": row["signal_strength"], "satellite_count": row["satellite_count"],
        "gps_status": row["gps_status"], "gps_odometer_km": f(row["gps_odometer_km"], 3),
        "location": row["location"] or "",
        "gap_seconds": f(row["gap_seconds"], 0),
        "flags": record_flags(row),
    }
