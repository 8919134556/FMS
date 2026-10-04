"""Trip analytics for the Trip Report downloads (PDF/Excel) and the MAP
popup's "Trip Analysis" panel.

Everything here is derived from the same two sources the on-screen Trip
Report already uses — nothing is stored, nothing is estimated beyond what
the data supports:

    apps.tracking.trip_report.compute_vehicle_trips()   -> which trips exist
                                                            (identical list,
                                                            same start/end/
                                                            distance as the page)
    TelemetryEvent (full-resolution GPS history)        -> what happened
                                                            inside each trip

For each trip the full, un-thinned history between its start and end is
walked once, interval by interval (reading i -> reading i+1), and every
interval is attributed to exactly one state:

    MOVING      ignition ON  and speed >= TELEMATICS_MOVEMENT_SPEED_THRESHOLD_KMH
    IDLE        ignition ON  and speed <  threshold      (engine running, standing)
    ENGINE_OFF  ignition OFF inside the vehicle's trip closure time (a short stop that
                did not end the trip — see trip_report's merge rule)
    NO_DATA     the device sent nothing for > MAX_SAMPLE_GAP, or sent no
                speed — never guessed as moving or idle

so ``moving + idle + engine_off + no_data`` always adds back up to the time
span of the recorded points, and every derived number (average speed, idle
share, utilization, …) can be traced back to real readings.

Distance: the trip's ``distance_km`` is the device odometer delta — the exact
figure the Trip Report page shows — and every distance TOTAL (summary,
per-vehicle, per-day) uses only that figure, so report totals always equal the
page's. When a trip has no odometer reading, its own row shows the GPS path
length instead (sum of great-circle hops between consecutive valid fixes,
ignoring physically impossible jumps), labelled "GPS", and the summary states
how much GPS-estimated distance was left out of the totals.

Performance: history is streamed from PostgreSQL with ``.iterator()`` in
``(vehicle, timestamp)`` order — the existing index — as lightweight tuples,
so memory stays bounded by the per-report output caps below, not by the
number of readings in the range. An up-front COUNT refuses selections too
large to process in one request, with a message telling the user how to
narrow them.
"""

import dataclasses
import datetime
import math
from collections import namedtuple
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from apps.core.models import SystemSettings
from apps.core.utils import display_timezone
from apps.tracking import trip_report
from apps.tracking.models import TelemetryEvent

# A reading gap longer than this is "no data", not a long idle/drive — the
# device was out of coverage or powered down, and we don't know what happened.
MAX_SAMPLE_GAP = datetime.timedelta(minutes=15)
# A standstill shorter than this is traffic, not a stop worth listing.
MIN_STOP = datetime.timedelta(minutes=1)
# Two consecutive fixes implying more than this are a GPS jump, not travel.
MAX_PLAUSIBLE_SPEED_KMH = 250

# Guard rails for one synchronous report request (see module docstring).
EXPORT_MAX_GPS_RECORDS = 1_500_000
# Only the single-trip (Trip Analysis) report lists GPS readings; the fleet
# report is summary/analysis only. Its Excel GPS History sheet holds up to
# EXCEL_MAX_GPS_ROWS; its PDF table is evenly sampled beyond PDF_MAX_GPS_ROWS_TRIP.
EXCEL_MAX_GPS_ROWS = 200_000
PDF_MAX_GPS_ROWS_TRIP = 1_000

STATE_MOVING = "moving"
STATE_IDLE = "idle"
STATE_ENGINE_OFF = "engine_off"
STATE_NO_DATA = "no_data"
STATE_LABELS = {
    STATE_MOVING: "Moving",
    STATE_IDLE: "Idling (engine on)",
    STATE_ENGINE_OFF: "Stopped (engine off)",
    STATE_NO_DATA: "No data",
}

DURATION_BUCKETS = (
    ("< 15 min", 0, 15 * 60),
    ("15–30 min", 15 * 60, 30 * 60),
    ("30–60 min", 30 * 60, 60 * 60),
    ("1–2 h", 60 * 60, 2 * 3600),
    ("2–4 h", 2 * 3600, 4 * 3600),
    ("> 4 h", 4 * 3600, None),
)

GpsPoint = namedtuple(
    "GpsPoint",
    "vehicle_id timestamp latitude longitude speed ignition odometer heading satellites location has_fix",
)
_POINT_FIELDS = (
    "vehicle_id", "timestamp", "latitude", "longitude", "speed", "ignition",
    "odometer", "heading", "satellite_count", "metadata__location",
)


class ReportError(Exception):
    """A user-facing reason a report can't be produced (bad input, nothing to
    report, selection too large). ``status`` is the HTTP status to answer with."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def moving_threshold_kmh():
    return Decimal(settings.TELEMATICS_MOVEMENT_SPEED_THRESHOLD_KMH)


def speed_bands():
    """Moving-time speed bands, starting at the movement threshold so the
    bands partition MOVING time exactly (idle time is reported separately)."""
    threshold = int(moving_threshold_kmh())
    edges = [threshold] + [e for e in (20, 40, 60, 80, 100) if e > threshold]
    bands = [(f"{lo}–{hi} km/h", lo, hi) for lo, hi in zip(edges, edges[1:])]
    bands.append((f"{edges[-1]}+ km/h", edges[-1], None))
    return bands


def _band_index(speed, bands):
    for index, (_label, lo, hi) in enumerate(bands):
        if speed >= lo and (hi is None or speed < hi):
            return index
    return 0


def haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = (math.radians(float(x)) for x in (lat1, lon1, lat2, lon2))
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(a)))


def _to_point(row):
    vehicle_id, ts, lat, lon, speed, ignition, odometer, heading, satellites, location = row
    return GpsPoint(
        vehicle_id, ts, lat, lon, speed, ignition, odometer, heading, satellites,
        location if isinstance(location, str) else "",
        trip_report._has_gps_fix(lat, lon),
    )


def _points_queryset(vehicle_ids, start, end):
    return TelemetryEvent.objects.filter(
        vehicle_id__in=vehicle_ids, timestamp__gte=start, timestamp__lte=end
    ).order_by("vehicle_id", "timestamp")


def stream_points(vehicle_ids, start, end):
    for row in _points_queryset(vehicle_ids, start, end).values_list(*_POINT_FIELDS).iterator(chunk_size=5000):
        yield _to_point(row)


# ---------------------------------------------------------------------------
# One trip, walked reading by reading
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Stop:
    start_at: datetime.datetime
    end_at: datetime.datetime
    latitude: Decimal | None
    longitude: Decimal | None
    location: str
    engine_off: bool  # ignition went OFF at some point during the stop

    @property
    def duration_seconds(self):
        return (self.end_at - self.start_at).total_seconds()


class TripAnalysis:
    """Accumulates one trip's metrics from its readings, fed in time order via
    ``add()``. ``keep_series`` retains the per-reading speed/distance series
    (single-trip reports and charts); the fleet report leaves it off."""

    def __init__(self, trip, *, now, keep_series=False):
        self.trip = trip
        self.now = now
        self.keep_series = keep_series
        self.threshold = moving_threshold_kmh()
        self.bands = speed_bands()

        self.point_count = 0
        self.fix_count = 0
        self.speed_count = 0
        self.state_seconds = dict.fromkeys(STATE_LABELS, 0.0)
        self.band_seconds = [0.0] * len(self.bands)
        self.gps_distance_km = 0.0
        self.max_speed = None
        self.max_speed_point = None
        self.first_fix = None
        self.last_fix = None
        self.first_point = None
        self.last_point = None
        self.stops = []
        self.series = []  # (timestamp, speed | None, cumulative_km | None)

        self._prev = None
        self._prev_fix = None
        self._stop = None  # open Stop being extended
        self._use_odometer = trip.distance_km is not None and trip.start_odometer is not None

    # -- feeding ----------------------------------------------------------

    def add(self, point):
        self.point_count += 1
        if self.first_point is None:
            self.first_point = point
        self.last_point = point
        if point.speed is not None:
            self.speed_count += 1
            if self.max_speed is None or point.speed > self.max_speed:
                self.max_speed, self.max_speed_point = point.speed, point

        prev = self._prev
        if prev is not None:
            seconds = (point.timestamp - prev.timestamp).total_seconds()
            if seconds > 0:
                state = self._interval_state(prev, seconds)
                self.state_seconds[state] += seconds
                if state == STATE_MOVING:
                    self.band_seconds[_band_index(prev.speed, self.bands)] += seconds
                if state in (STATE_IDLE, STATE_ENGINE_OFF):
                    self._extend_stop(prev, point, engine_off=state == STATE_ENGINE_OFF)
                else:
                    self._close_stop()
        if point.has_fix:
            self.fix_count += 1
            if self.first_fix is None:
                self.first_fix = point
            if self._prev_fix is not None:
                hop = haversine_km(self._prev_fix.latitude, self._prev_fix.longitude, point.latitude, point.longitude)
                hours = (point.timestamp - self._prev_fix.timestamp).total_seconds() / 3600
                if hours > 0 and hop / hours <= MAX_PLAUSIBLE_SPEED_KMH:
                    self.gps_distance_km += hop
                    self._prev_fix = point
                # else: a GPS jump — keep measuring from the last trustworthy fix
            else:
                self._prev_fix = point
            self.last_fix = point

        self._prev = point

        if self.keep_series:
            self.series.append((point.timestamp, point.speed, self._cumulative_km(point)))

    def _interval_state(self, prev, seconds):
        if seconds > MAX_SAMPLE_GAP.total_seconds():
            return STATE_NO_DATA
        if prev.ignition is False:
            return STATE_ENGINE_OFF
        if prev.speed is None:
            return STATE_NO_DATA
        return STATE_MOVING if prev.speed >= self.threshold else STATE_IDLE

    def _extend_stop(self, prev, point, *, engine_off):
        if self._stop is None:
            anchor = prev if prev.has_fix else self.last_fix
            self._stop = Stop(
                start_at=prev.timestamp,
                end_at=point.timestamp,
                latitude=anchor.latitude if anchor else None,
                longitude=anchor.longitude if anchor else None,
                location=(anchor.location if anchor else "") or "",
                engine_off=engine_off,
            )
        else:
            self._stop.end_at = point.timestamp
            self._stop.engine_off = self._stop.engine_off or engine_off

    def _close_stop(self):
        if self._stop is not None and self._stop.duration_seconds >= MIN_STOP.total_seconds():
            self.stops.append(self._stop)
        self._stop = None

    def _cumulative_km(self, point):
        if self._use_odometer:
            if point.odometer is None:
                return None
            return max(0.0, float(point.odometer - self.trip.start_odometer))
        return self.gps_distance_km

    def finish(self):
        self._close_stop()
        return self

    # -- derived metrics --------------------------------------------------

    @property
    def duration_seconds(self):
        return self.trip.duration(now=self.now).total_seconds()

    @property
    def distance_km(self):
        """Odometer distance (what the Trip Report page shows) when known,
        else the GPS path length — see ``distance_source``."""
        if self.trip.distance_km is not None:
            return float(self.trip.distance_km)
        if self.fix_count >= 2:
            return self.gps_distance_km
        return None

    @property
    def odometer_km(self):
        """The Trip Report page's own distance figure (None when the device
        reported no usable odometer) — the only distance that goes into totals."""
        return float(self.trip.distance_km) if self.trip.distance_km is not None else None

    @property
    def distance_source(self):
        if self.trip.distance_km is not None:
            return "Odometer"
        return "GPS" if self.fix_count >= 2 else ""

    @property
    def moving_seconds(self):
        return self.state_seconds[STATE_MOVING]

    @property
    def idle_seconds(self):
        return self.state_seconds[STATE_IDLE]

    @property
    def engine_off_seconds(self):
        return self.state_seconds[STATE_ENGINE_OFF]

    @property
    def no_data_seconds(self):
        return self.state_seconds[STATE_NO_DATA]

    @property
    def avg_speed_kmh(self):
        """Trip-average speed: distance over the whole trip duration (stops included)."""
        distance, seconds = self.distance_km, self.duration_seconds
        return distance / (seconds / 3600) if distance is not None and seconds > 0 else None

    @property
    def avg_moving_speed_kmh(self):
        """Distance over MOVING time — only when that is physically consistent.
        If the odometer advanced while the device kept reporting speeds below
        the moving threshold (a crawl, or unreliable speed data), moving time
        is too small to divide by and the result would exceed the fastest
        speed actually recorded; such a figure is withheld, never published."""
        distance, seconds = self.distance_km, self.moving_seconds
        if distance is None or seconds <= 0:
            return None
        speed = distance / (seconds / 3600)
        if self.max_speed is not None and speed > float(self.max_speed) * 1.05:
            return None
        return speed

    @property
    def longest_stop(self):
        return max(self.stops, key=lambda s: s.duration_seconds, default=None)

    @property
    def max_speed_kmh(self):
        return float(self.max_speed) if self.max_speed is not None else None


# ---------------------------------------------------------------------------
# Report containers
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class TripRecord:
    trip: trip_report.DetectedTrip
    trip_id: str
    driver_name: str
    analysis: TripAnalysis

    @property
    def vehicle(self):
        return self.trip.vehicle


@dataclasses.dataclass
class ReportMeta:
    title: str
    subtitle: str
    company: str
    app_name: str
    tz: datetime.tzinfo
    generated_at: datetime.datetime
    period_start: datetime.datetime
    period_end: datetime.datetime
    period_label: str
    filters_label: str
    moving_threshold_kmh: Decimal
    closure_label: str  # the trip closure time(s) of the vehicles covered, e.g. "10 min"

    @property
    def tz_name(self):
        return getattr(self.tz, "key", str(self.tz))


def closure_label(vehicles):
    """Human description of the per-vehicle trip closure times in a report:
    "10 min" when every vehicle uses the same value, else the range."""
    values = sorted({int(trip_report.trip_closure_window(v).total_seconds() // 60) for v in vehicles})
    if not values:
        return ""
    if len(values) == 1:
        return f"{values[0]} min"
    return f"{values[0]}–{values[-1]} min, set per vehicle"


def _meta(*, title, subtitle, period_start, period_end, period_label, filters_label, now, tz, vehicles):
    company = SystemSettings.load().company_name or getattr(settings, "FMS_COMPANY_NAME", "")
    app_name = getattr(settings, "FMS_APP_NAME", "FMS Admin Portal")
    return ReportMeta(
        title=title,
        subtitle=subtitle,
        company=company or app_name,
        app_name=app_name,
        tz=tz,
        generated_at=now.astimezone(tz),
        period_start=period_start.astimezone(tz),
        period_end=period_end.astimezone(tz),
        period_label=period_label,
        filters_label=filters_label,
        moving_threshold_kmh=moving_threshold_kmh(),
        closure_label=closure_label(vehicles),
    )


def driver_label(driver):
    return driver.get_full_name() if driver else ""


# ---------------------------------------------------------------------------
# Period (fleet) report
# ---------------------------------------------------------------------------

RANGE_LABELS = {
    "today": "Today",
    "yesterday": "Yesterday",
    "last3": "Last 3 Days",
    "last5": "Last 5 Days",
    "last7": "Last 7 Days",
    "custom": "Custom Range",
}


def validate_export_range(range_key, from_str, to_str, *, now, tz):
    """Like ``trip_report.resolve_date_range`` but strict: a download must
    never silently cover a different period than the user asked for, so bad
    input is rejected with a clear message instead of falling back to today."""
    range_key = (range_key or "today").strip().lower()
    if range_key not in RANGE_LABELS:
        raise ReportError("Unknown date range. Choose Today, Yesterday, 3, 5 or 7 days, or a custom range.")
    if range_key != "custom":
        return range_key, *trip_report.resolve_date_range(range_key, None, None, now=now, tz=tz)
    if not from_str or not to_str:
        raise ReportError("Select both a start date and an end date for the custom range.")
    try:
        start = datetime.date.fromisoformat(from_str)
        end = datetime.date.fromisoformat(to_str)
    except ValueError:
        raise ReportError("The custom range dates are not valid (expected YYYY-MM-DD).") from None
    today = now.astimezone(tz).date()
    if start > end:
        raise ReportError("The start date must be on or before the end date.")
    if start > today:
        raise ReportError("The custom range starts in the future — there is no history to report yet.")
    if (end - start).days + 1 > trip_report.MAX_CUSTOM_RANGE_DAYS:
        raise ReportError(
            f"A report can cover at most {trip_report.MAX_CUSTOM_RANGE_DAYS} days. Please choose a shorter range."
        )
    return range_key, start, min(end, today)


@dataclasses.dataclass
class PeriodReport:
    meta: ReportMeta
    start_date: datetime.date
    end_date: datetime.date
    vehicles: list
    trips: list  # TripRecord, newest first (same order as the page)
    vehicle_rows: list
    day_rows: list
    hour_counts: list
    duration_buckets: list
    band_labels: list
    band_seconds: list
    stops: list  # (TripRecord, Stop)
    summary: dict
    insights: list  # (label, value, detail)
    # A count only. The fleet report is a summary/analysis report and never
    # carries per-reading GPS rows; those belong to the single-trip report
    # (SingleTripReport.points).
    gps_total: int


def build_period_report(*, user, range_key, from_str="", to_str="", vehicle_uuid="", search="", now=None):
    now = now or timezone.now()
    tz = display_timezone()
    range_key, start_date, end_date = validate_export_range(range_key, from_str, to_str, now=now, tz=tz)

    vehicles = trip_report.scoped_tracked_vehicles(user, vehicle_uuid=vehicle_uuid, search=search)
    if vehicle_uuid and not vehicles:
        raise ReportError("The selected vehicle was not found or has no GPS device.", status=404)
    if not vehicles:
        raise ReportError("No GPS-tracked vehicles match the current filters.", status=404)

    # Exactly the trips the page shows for the same filters.
    trips, _ = trip_report.compute_vehicle_trips(
        user=user, start_date=start_date, end_date=end_date, vehicle_uuid=vehicle_uuid, search=search, now=now
    )

    period_start = datetime.datetime.combine(start_date, datetime.time.min, tzinfo=tz)
    period_end = min(datetime.datetime.combine(end_date + datetime.timedelta(days=1), datetime.time.min, tzinfo=tz), now)
    vehicle_ids = [v.id for v in vehicles]

    window_start = min([period_start] + [t.start_at for t in trips])
    window_end = max([period_end] + [(t.end_at or now) for t in trips])
    total_points = _points_queryset(vehicle_ids, window_start, window_end).count()
    if total_points > EXPORT_MAX_GPS_RECORDS:
        raise ReportError(
            f"This selection contains {total_points:,} GPS records — too many for one report. "
            "Select a single vehicle or a shorter date range.",
            status=413,
        )
    if not trips and total_points == 0:
        raise ReportError("No trips or GPS history were recorded for the selected vehicles and period.", status=404)

    resolve_driver = trip_report.driver_resolver(vehicles)
    records = [
        TripRecord(
            trip=t,
            trip_id=trip_report.trip_identifier(t, tz=tz),
            driver_name=driver_label(resolve_driver(t.vehicle, t.start_at.astimezone(tz).date())),
            analysis=TripAnalysis(t, now=now),
        )
        for t in trips
    ]

    # Walk the history once, handing each reading to the trip it belongs to
    # (for the per-trip analysis) and counting the in-period readings.
    records_by_vehicle = {}
    for record in sorted(records, key=lambda r: r.trip.start_at):
        records_by_vehicle.setdefault(record.vehicle.id, []).append(record)
    cursor = {}
    gps_total = 0

    for point in stream_points(vehicle_ids, window_start, window_end):
        vehicle_records = records_by_vehicle.get(point.vehicle_id, ())
        index = cursor.get(point.vehicle_id, 0)
        while index < len(vehicle_records) and (vehicle_records[index].trip.end_at or now) < point.timestamp:
            index += 1
        cursor[point.vehicle_id] = index
        if index < len(vehicle_records) and vehicle_records[index].trip.start_at <= point.timestamp:
            vehicle_records[index].analysis.add(point)
        if period_start <= point.timestamp <= period_end:
            gps_total += 1

    for record in records:
        record.analysis.finish()

    period_label = RANGE_LABELS[range_key]
    date_span = (
        f"{start_date:%d %b %Y}" if start_date == end_date else f"{start_date:%d %b %Y} – {end_date:%d %b %Y}"
    )
    filters = []
    if vehicle_uuid:
        filters.append(f"Vehicle: {vehicles[0].registration_number}")
    if search.strip():
        filters.append(f"Search: “{search.strip()}”")
    meta = _meta(
        title="Fleet Trip Report",
        subtitle=f"{period_label} · {date_span}",
        period_start=period_start,
        period_end=period_end,
        period_label=f"{period_label} ({date_span})",
        filters_label=", ".join(filters) or "All tracked vehicles",
        now=now,
        tz=tz,
        vehicles=vehicles,
    )
    report = PeriodReport(
        meta=meta, start_date=start_date, end_date=end_date, vehicles=vehicles, trips=records,
        vehicle_rows=[], day_rows=[], hour_counts=[0] * 24, duration_buckets=[], band_labels=[],
        band_seconds=[], stops=[], summary={}, insights=[], gps_total=gps_total,
    )
    _rollup(report, resolve_driver, now=now)
    return report


def _sum(values):
    values = [v for v in values if v is not None]
    return sum(values) if values else None


def _max(values):
    values = [v for v in values if v is not None]
    return max(values) if values else None


def _speed(distance_km, seconds):
    return distance_km / (seconds / 3600) if distance_km is not None and seconds and seconds > 0 else None


def _odometer_speed(analyses):
    """Average speed over the trips that HAVE an odometer distance — distance
    and time from the same trips, so a trip with no distance can't dilute it."""
    measured = [a for a in analyses if a.odometer_km is not None]
    return _speed(_sum(a.odometer_km for a in measured), sum(a.duration_seconds for a in measured))


def _rollup(report, resolve_driver, *, now):
    tz = report.meta.tz
    records = report.trips
    elapsed_seconds = max((report.meta.period_end - report.meta.period_start).total_seconds(), 1)

    # -- per vehicle --
    by_vehicle = {}
    for record in records:
        by_vehicle.setdefault(record.vehicle.id, []).append(record)
    for vehicle in sorted(report.vehicles, key=lambda v: v.registration_number):
        vr = by_vehicle.get(vehicle.id, [])
        analyses = [r.analysis for r in vr]
        distance = _sum(a.odometer_km for a in analyses)
        travel = sum(a.duration_seconds for a in analyses)
        latest = max(vr, key=lambda r: r.trip.start_at, default=None)
        report.vehicle_rows.append({
            "vehicle": vehicle,
            "registration_number": vehicle.registration_number,
            "vehicle_type": vehicle.vehicle_type.name if vehicle.vehicle_type_id else "",
            "driver": latest.driver_name if latest else driver_label(resolve_driver(vehicle, report.end_date)),
            "trips": len(vr),
            "completed": sum(1 for r in vr if r.trip.status == "COMPLETED"),
            "distance_km": distance,
            "travel_seconds": travel,
            "moving_seconds": sum(a.moving_seconds for a in analyses),
            "idle_seconds": sum(a.idle_seconds for a in analyses),
            "engine_off_seconds": sum(a.engine_off_seconds for a in analyses),
            "stops": sum(len(a.stops) for a in analyses),
            "avg_speed_kmh": _odometer_speed(analyses),
            "max_speed_kmh": _max(a.max_speed_kmh for a in analyses),
            "first_start": min((r.trip.start_at for r in vr), default=None),
            "last_end": max((r.trip.end_at or now for r in vr), default=None) if vr else None,
            "longest_trip_seconds": _max(a.duration_seconds for a in analyses),
            "utilization_pct": min(100.0, travel / elapsed_seconds * 100),
            "gps_points": sum(a.point_count for a in analyses),
        })

    # -- per day (trips bucketed by local start date, as on the page) --
    by_day = {}
    for record in records:
        by_day.setdefault(record.trip.start_at.astimezone(tz).date(), []).append(record)
    day = report.start_date
    while day <= report.end_date:
        dr = by_day.get(day, [])
        analyses = [r.analysis for r in dr]
        distance = _sum(a.odometer_km for a in analyses)
        travel = sum(a.duration_seconds for a in analyses)
        report.day_rows.append({
            "date": day,
            "trips": len(dr),
            "vehicles": len({r.vehicle.id for r in dr}),
            "distance_km": distance,
            "travel_seconds": travel,
            "moving_seconds": sum(a.moving_seconds for a in analyses),
            "idle_seconds": sum(a.idle_seconds for a in analyses),
            "avg_speed_kmh": _odometer_speed(analyses),
            "max_speed_kmh": _max(a.max_speed_kmh for a in analyses),
        })
        day += datetime.timedelta(days=1)
    # An ACTIVE trip that began before the range is listed (as on the page)
    # but has no day row of its own inside the range.
    earlier = [r for r in records if r.trip.start_at.astimezone(tz).date() < report.start_date]

    for record in records:
        report.hour_counts[record.trip.start_at.astimezone(tz).hour] += 1

    durations = [r.analysis.duration_seconds for r in records]
    report.duration_buckets = [
        (label, sum(1 for d in durations if d >= lo and (hi is None or d < hi))) for label, lo, hi in DURATION_BUCKETS
    ]

    bands = speed_bands()
    report.band_labels = [b[0] for b in bands]
    report.band_seconds = [sum(r.analysis.band_seconds[i] for r in records) for i in range(len(bands))]

    report.stops = sorted(
        ((r, s) for r in records for s in r.analysis.stops), key=lambda rs: rs[1].start_at
    )

    # -- summary --
    analyses = [r.analysis for r in records]
    total_distance = _sum(a.odometer_km for a in analyses)
    gps_only = [a for a in analyses if a.odometer_km is None and a.distance_km is not None]
    with_odometer = sum(1 for a in analyses if a.odometer_km is not None)
    travel = sum(a.duration_seconds for a in analyses)
    moving = sum(a.moving_seconds for a in analyses)
    idle = sum(a.idle_seconds for a in analyses)
    top = max(records, key=lambda r: r.analysis.max_speed_kmh or -1, default=None)
    report.summary = {
        "total_vehicles": len(report.vehicles),
        "vehicles_with_trips": len(by_vehicle),
        "total_trips": len(records),
        "completed_trips": sum(1 for r in records if r.trip.status == "COMPLETED"),
        "active_trips": sum(1 for r in records if r.trip.status == "ACTIVE"),
        "earlier_active_trips": len(earlier),
        "total_distance_km": total_distance,
        "gps_distance_trips": len(gps_only),
        "gps_estimated_km": sum(a.distance_km for a in gps_only) if gps_only else None,
        "no_distance_trips": sum(1 for a in analyses if a.distance_km is None),
        "travel_seconds": travel,
        "moving_seconds": moving,
        "idle_seconds": idle,
        "engine_off_seconds": sum(a.engine_off_seconds for a in analyses),
        "no_data_seconds": sum(a.no_data_seconds for a in analyses),
        "avg_trip_seconds": travel / len(records) if records else None,
        "avg_trip_km": total_distance / with_odometer if with_odometer and total_distance is not None else None,
        "avg_speed_kmh": _odometer_speed(analyses),
        # Only trips whose moving speed is consistent (see TripAnalysis.avg_moving_speed_kmh).
        "avg_moving_speed_kmh": _speed(
            _sum(a.odometer_km for a in analyses if a.odometer_km is not None and a.avg_moving_speed_kmh is not None),
            sum(a.moving_seconds for a in analyses if a.odometer_km is not None and a.avg_moving_speed_kmh is not None),
        ),
        "max_speed_kmh": top.analysis.max_speed_kmh if top and top.analysis.max_speed_kmh is not None else None,
        "max_speed_record": top if top and top.analysis.max_speed_kmh is not None else None,
        "stops": len(report.stops),
        "gps_points": report.gps_total,
        "elapsed_seconds": elapsed_seconds,
        "fleet_utilization_pct": (
            min(100.0, travel / (elapsed_seconds * len(report.vehicles)) * 100) if report.vehicles else None
        ),
    }
    report.insights = _period_insights(report)


def _period_insights(report):
    """Plain-language findings — each one only when the data behind it exists."""
    from apps.tracking.trip_exports import fmt_duration, fmt_km, fmt_speed  # formatting only

    tz = report.meta.tz
    s = report.summary
    records = report.trips
    insights = []
    if not records:
        insights.append(("Trips", "None detected", "No ignition-on journeys were recorded in this period."))
        return insights

    active_rows = [r for r in report.vehicle_rows if r["trips"]]
    most_trips = max(active_rows, key=lambda r: r["trips"])
    ties = [r["registration_number"] for r in active_rows if r["trips"] == most_trips["trips"]]
    insights.append((
        "Most active vehicle", ", ".join(ties),
        f"{most_trips['trips']} trip{'s' if most_trips['trips'] != 1 else ''} in the period.",
    ))
    with_distance = [r for r in active_rows if r["distance_km"] is not None]
    if with_distance:
        top = max(with_distance, key=lambda r: r["distance_km"])
        share = f" — {top['distance_km'] / s['total_distance_km'] * 100:.0f}% of fleet distance" if s["total_distance_km"] else ""
        insights.append(("Highest-distance vehicle", top["registration_number"], f"{fmt_km(top['distance_km'])}{share}."))
    if len(active_rows) >= 1 and report.vehicle_rows:
        best = max(report.vehicle_rows, key=lambda r: r["utilization_pct"])
        worst = min(report.vehicle_rows, key=lambda r: r["utilization_pct"])
        insights.append((
            "Vehicle utilization",
            f"{s['fleet_utilization_pct']:.1f}% fleet average",
            f"Highest: {best['registration_number']} ({best['utilization_pct']:.1f}% of the period in trips)"
            + (f"; lowest: {worst['registration_number']} ({worst['utilization_pct']:.1f}%)." if worst is not best else "."),
        ))
    if s["max_speed_record"] is not None:
        r = s["max_speed_record"]
        p = r.analysis.max_speed_point
        where = f" near {p.location}" if p and p.location else ""
        insights.append((
            "Maximum recorded speed", fmt_speed(s["max_speed_kmh"]),
            f"{r.vehicle.registration_number} at {p.timestamp.astimezone(tz):%d %b %Y %H:%M}{where}.",
        ))
    if s["avg_speed_kmh"] is not None:
        insights.append((
            "Average speed", fmt_speed(s["avg_speed_kmh"]),
            f"Over total trip time; {fmt_speed(s['avg_moving_speed_kmh'])} while actually moving."
            if s["avg_moving_speed_kmh"] is not None else "Over total trip time.",
        ))
    longest = max(records, key=lambda r: r.analysis.duration_seconds)
    insights.append((
        "Longest trip", fmt_duration(longest.analysis.duration_seconds),
        f"{longest.trip_id} ({fmt_km(longest.analysis.distance_km)})"
        + (" — still in progress." if longest.trip.status == "ACTIVE" else "."),
    ))
    completed = [r for r in records if r.trip.status == "COMPLETED"]
    if len(completed) >= 2:
        shortest = min(completed, key=lambda r: r.analysis.duration_seconds)
        insights.append((
            "Shortest completed trip", fmt_duration(shortest.analysis.duration_seconds),
            f"{shortest.trip_id} ({fmt_km(shortest.analysis.distance_km)}).",
        ))
    with_km = [r for r in records if r.analysis.odometer_km is not None]
    if with_km:
        far = max(with_km, key=lambda r: r.analysis.odometer_km)
        insights.append(("Longest-distance trip", fmt_km(far.analysis.odometer_km), f"{far.trip_id}."))
    busiest = max(report.day_rows, key=lambda d: d["trips"])
    if busiest["trips"] and len(report.day_rows) > 1:
        insights.append(("Busiest day", f"{busiest['date']:%a %d %b %Y}", f"{busiest['trips']} trips."))
    if any(report.hour_counts):
        peak = max(range(24), key=lambda h: report.hour_counts[h])
        insights.append((
            "Peak start hour", f"{peak:02d}:00–{(peak + 1) % 24:02d}:00",
            f"{report.hour_counts[peak]} of {len(records)} trips started in this hour.",
        ))
    engine_on = s["moving_seconds"] + s["idle_seconds"]
    if engine_on > 0:
        idle_pct = s["idle_seconds"] / engine_on * 100
        insights.append((
            "Idling share", f"{idle_pct:.1f}% of engine-on time",
            f"{fmt_duration(s['idle_seconds'])} idling vs {fmt_duration(s['moving_seconds'])} moving"
            + (" — above 20%, worth reviewing for fuel waste." if idle_pct > 20 else "."),
        ))
    if report.stops:
        record, stop = max(report.stops, key=lambda rs: rs[1].duration_seconds)
        where = f" at {stop.location}" if stop.location else ""
        insights.append((
            "Stoppages", f"{len(report.stops)} stops ≥ {int(MIN_STOP.total_seconds() // 60)} min",
            f"Longest: {fmt_duration(stop.duration_seconds)} ({record.vehicle.registration_number}{where}).",
        ))
    typical = max(report.duration_buckets, key=lambda b: b[1])
    # Only a finding when one length bucket genuinely dominates.
    if typical[1] >= 2 and sum(1 for b in report.duration_buckets if b[1] == typical[1]) == 1:
        insights.append((
            "Typical trip length", typical[0],
            f"{typical[1]} of {len(records)} trips; average {fmt_duration(s['avg_trip_seconds'])}.",
        ))
    if s["no_data_seconds"] > 0:
        insights.append((
            "Data gaps", fmt_duration(s["no_data_seconds"]),
            f"Time inside trips with no reading for > {int(MAX_SAMPLE_GAP.total_seconds() // 60)} min; "
            "excluded from moving/idle totals.",
        ))
    return insights


# ---------------------------------------------------------------------------
# Single trip
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class SingleTripReport:
    meta: ReportMeta
    record: TripRecord
    points: list  # every GpsPoint of the trip, in order
    insights: list


def find_trip(*, user, vehicle_uuid, start_at, now):
    """Re-detects the trip from history (never trusting client-sent end
    time/distance) and returns the one that started at exactly ``start_at``."""
    tz = display_timezone()
    day = start_at.astimezone(tz).date()
    trips, _ = trip_report.compute_vehicle_trips(
        user=user, start_date=day, end_date=day, vehicle_uuid=vehicle_uuid, now=now
    )
    return next((t for t in trips if t.start_at == start_at), None)


def build_trip_report(*, user, vehicle_uuid, start_at, now=None, keep_points=True):
    now = now or timezone.now()
    tz = display_timezone()
    trip = find_trip(user=user, vehicle_uuid=vehicle_uuid, start_at=start_at, now=now)
    if trip is None:
        raise ReportError(
            "This trip could not be found. It may belong to a vehicle you can no longer access — "
            "refresh the Trip Report and try again.",
            status=404,
        )
    end = trip.end_at or now
    total = _points_queryset([trip.vehicle.id], trip.start_at, end).count()
    if total > EXPORT_MAX_GPS_RECORDS:
        raise ReportError(f"This trip has {total:,} GPS records — too many to include in one report.", status=413)

    driver = trip_report.resolve_trip_drivers([trip], tz=tz).get(id(trip))
    analysis = TripAnalysis(trip, now=now, keep_series=True)
    points = []
    for point in stream_points([trip.vehicle.id], trip.start_at, end):
        analysis.add(point)
        if keep_points:
            points.append(point)
    analysis.finish()

    record = TripRecord(trip=trip, trip_id=trip_report.trip_identifier(trip, tz=tz),
                        driver_name=driver_label(driver), analysis=analysis)
    start_local = trip.start_at.astimezone(tz)
    meta = _meta(
        title="Trip Analysis Report",
        subtitle=f"{trip.vehicle.registration_number} · {start_local:%d %b %Y %H:%M}",
        period_start=trip.start_at,
        period_end=end,
        period_label=f"{start_local:%d %b %Y %H:%M:%S} → "
        + (f"{trip.end_at.astimezone(tz):%d %b %Y %H:%M:%S}" if trip.end_at else "In progress"),
        filters_label=f"Trip {record.trip_id}",
        now=now,
        tz=tz,
        vehicles=[trip.vehicle],
    )
    return SingleTripReport(meta=meta, record=record, points=points, insights=_trip_insights(record, tz))


def _trip_insights(record, tz):
    from apps.tracking.trip_exports import fmt_duration, fmt_km, fmt_speed

    a = record.analysis
    out = []
    if a.point_count == 0:
        return [("GPS history", "No readings", "No GPS records were stored for this trip's time window.")]
    engine_on = a.moving_seconds + a.idle_seconds
    if engine_on > 0:
        out.append((
            "Time in motion", f"{a.moving_seconds / engine_on * 100:.0f}% of engine-on time",
            f"{fmt_duration(a.moving_seconds)} moving, {fmt_duration(a.idle_seconds)} idling.",
        ))
    if a.avg_moving_speed_kmh is not None:
        out.append(("Average moving speed", fmt_speed(a.avg_moving_speed_kmh),
                    f"Trip average including stops: {fmt_speed(a.avg_speed_kmh)}."))
    if a.max_speed_point is not None:
        p = a.max_speed_point
        out.append(("Top speed", fmt_speed(a.max_speed_kmh),
                    f"At {p.timestamp.astimezone(tz):%H:%M:%S}" + (f" near {p.location}." if p.location else ".")))
    if a.stops:
        ls = a.longest_stop
        out.append(("Stops", f"{len(a.stops)} (≥ {int(MIN_STOP.total_seconds() // 60)} min)",
                    f"Longest {fmt_duration(ls.duration_seconds)}" + (f" at {ls.location}." if ls.location else ".")))
    if a.trip.distance_km is not None and a.fix_count >= 2 and a.gps_distance_km > 0:
        diff = abs(a.gps_distance_km - float(a.trip.distance_km))
        out.append(("Distance cross-check", f"GPS path {fmt_km(a.gps_distance_km)}",
                    f"Odometer {fmt_km(float(a.trip.distance_km))} (difference {fmt_km(diff)})."))
    if a.point_count - a.fix_count:
        out.append(("GPS fix quality", f"{a.fix_count} of {a.point_count} readings with a fix",
                    "Readings without a satellite fix are listed but not mapped."))
    if a.no_data_seconds:
        out.append(("Data gaps", fmt_duration(a.no_data_seconds),
                    f"No reading for > {int(MAX_SAMPLE_GAP.total_seconds() // 60)} min; excluded from moving/idle time."))
    return out


def trip_analysis_payload(report):
    """JSON-ready summary for the MAP popup (no per-reading data)."""
    r = report.record
    a = r.analysis
    t = r.trip

    def iso(dt):
        return dt.isoformat() if dt else None

    def num(value, places=1):
        return round(float(value), places) if value is not None else None

    return {
        "trip_id": r.trip_id,
        "vehicle_uuid": str(t.vehicle.uuid),
        "registration_number": t.vehicle.registration_number,
        "driver_name": r.driver_name or None,
        "status": t.status,
        "start_time": iso(t.start_at),
        "end_time": iso(t.end_at),
        "start_location": t.start_location,
        "end_location": t.end_location,
        "start_latitude": num(t.start_latitude, 6),
        "start_longitude": num(t.start_longitude, 6),
        "end_latitude": num(t.end_latitude, 6),
        "end_longitude": num(t.end_longitude, 6),
        "duration_seconds": int(a.duration_seconds),
        "distance_km": num(a.distance_km, 2),
        "distance_source": a.distance_source,
        "gps_distance_km": num(a.gps_distance_km, 2) if a.fix_count >= 2 else None,
        "moving_seconds": int(a.moving_seconds),
        "idle_seconds": int(a.idle_seconds),
        "engine_off_seconds": int(a.engine_off_seconds),
        "no_data_seconds": int(a.no_data_seconds),
        "avg_speed_kmh": num(a.avg_speed_kmh),
        "avg_moving_speed_kmh": num(a.avg_moving_speed_kmh),
        "max_speed_kmh": num(a.max_speed_kmh),
        "max_speed_time": iso(a.max_speed_point.timestamp) if a.max_speed_point else None,
        "gps_points": a.point_count,
        "gps_fix_points": a.fix_count,
        "stop_count": len(a.stops),
        "longest_stop_seconds": int(a.longest_stop.duration_seconds) if a.longest_stop else None,
        "stops": [
            {
                "start_time": iso(s.start_at),
                "end_time": iso(s.end_at),
                "duration_seconds": int(s.duration_seconds),
                "location": s.location,
                "latitude": num(s.latitude, 6),
                "longitude": num(s.longitude, 6),
                "engine_off": s.engine_off,
            }
            for s in sorted(a.stops, key=lambda s: s.duration_seconds, reverse=True)[:10]
        ],
        "moving_threshold_kmh": int(report.meta.moving_threshold_kmh),
        "timezone": report.meta.tz_name,
        "generated_at": iso(report.meta.generated_at),
        "insights": [{"label": lbl, "value": val, "detail": det} for lbl, val, det in report.insights],
    }
