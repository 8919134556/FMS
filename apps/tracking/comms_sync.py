"""Bridge: Zentora comms "App DB" -> FMS tracking tables.

The comms program (TCP server -> SQLite buffer -> worker) parses Teltonika
packets and writes them, through stored procedures, to two PostgreSQL
databases: a Raw DB and an App DB (``current_table`` = latest position per
unit, ``history_table`` = every reading). The FMS live map, dashboards and
client views read the FMS ``tracking_*`` tables instead, so without this
bridge a vehicle can be perfectly healthy in comms and invisible in FMS.

    device -> comms (Raw DB, App DB) -> [this bridge] -> FMS tracking tables
                                                          -> map / dashboard

Mapping is 100% data-driven and never uses the comms program's own client or
vehicle columns: a comms ``unitno`` (the device IMEI) is looked up in
``TrackingDevice.imei``; that device's vehicle and that vehicle's client —
as registered in FMS — decide who owns the reading. An IMEI with no FMS
device is skipped and reported; nothing is created automatically.

Reads are read-only and incremental (``CommsSyncState.last_id``); writes go
through ``TelemetryIngestionService.ingest_events`` so client stamping,
dedup, the live-position upsert and geofence checks are the same code the
socket/HTTP paths use.
"""

import datetime
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings
from django.utils import timezone

from apps.core.utils import display_timezone
from apps.tracking.models import CommsSyncState, TrackingDevice
from apps.tracking.providers.base import NormalizedEvent
from apps.tracking.services import TelemetryIngestionService

logger = logging.getLogger(__name__)

STATE_KEY = "app_history"
# Session-level PostgreSQL advisory lock ("ZENTORA"), so the auto-started
# background thread, extra web workers and `manage.py sync_comms_data` never
# run a pass at the same time — whoever holds it syncs, the rest skip.
ADVISORY_LOCK_KEY = 0x5A454E544F5241
DEFAULT_BATCH_SIZE = 2000
DEFAULT_MAX_ROWS = 50000
DEFAULT_HISTORY_DAYS = 7

_COLUMNS = (
    "id, unitno, tracktime, lat, lon, speed, direction, ignition, odometer, gpsodometer, gpsstatus, location, panic"
)

# analog1 (mV) of the panic readings, from the comms Raw DB — the value the App
# DB's stored procedure turned into ``panic`` (>= 10 V). Matched on the device
# and its own timestamp. Runs only for batches that contain panic readings.
_PANIC_VOLTAGE_SQL = """
SELECT DISTINCT ON (r.unitno, r.tracktime) r.unitno, r.tracktime, r.analog1
FROM teltonika_raw_table r
JOIN unnest(%s::text[], %s::timestamp[]) AS k(unitno, tracktime) ON r.unitno = k.unitno AND r.tracktime = k.tracktime
WHERE r.tracktime BETWEEN %s AND %s
ORDER BY r.unitno, r.tracktime, r.id DESC
"""


@dataclass
class SyncReport:
    current_rows: int = 0
    history_rows: int = 0
    accepted: int = 0
    rejected: int = 0
    unknown_units: dict = field(default_factory=lambda: defaultdict(int))
    inactive_units: dict = field(default_factory=lambda: defaultdict(int))
    last_history_id: int = 0

    def as_dict(self):
        return {
            "current_rows": self.current_rows, "history_rows": self.history_rows,
            "accepted": self.accepted, "rejected": self.rejected,
            "unknown_units": dict(self.unknown_units), "inactive_units": dict(self.inactive_units),
            "last_history_id": self.last_history_id,
        }


def _local_timezone():
    """comms stores ``tracktime`` as naive *local* time (its worker converts
    UTC -> country time before inserting), so interpret it in the org-wide
    display timezone (Settings > Localization, default Asia/Kolkata)."""
    return display_timezone()


def _decimal(value, places):
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def row_to_event(row, tz):
    """One comms row -> ``NormalizedEvent`` (or ``None`` if it has no usable
    time/position). Field notes:

    * ``direction`` is a string of degrees; ``ignition`` is 0/1.
    * ``odometer`` is the Teltonika total-distance counter in **metres**;
      FMS stores kilometres. ``gpsodometer`` is the trip odometer (also
      metres), carried in ``metadata`` and shown as "GPS Odometer" (km).
    * ``location`` (reverse-geocoded address) is kept in ``metadata``.
    * ``panic`` (0/1, decided by the comms stored procedure from analog1 >= 10 V)
      is kept, unchanged, in ``metadata["panic"]`` — the input of
      apps.alerts.events. NULL (rows from before comms computed it) is omitted.
      ``panic_voltage`` (analog1 in volts) is added when the Raw DB was read.
    """
    tracktime, lat, lon = row.get("tracktime"), row.get("lat"), row.get("lon")
    if tracktime is None or lat is None or lon is None:
        return None
    if tracktime.tzinfo is None:
        tracktime = tracktime.replace(tzinfo=tz)

    heading = None
    try:
        if row.get("direction") not in (None, ""):
            heading = int(float(row["direction"])) % 360
    except (TypeError, ValueError):
        heading = None

    odometer_m = row.get("odometer")
    gps_odometer_m = row.get("gpsodometer")
    return NormalizedEvent(
        timestamp=tracktime,
        latitude=_decimal(lat, 6),
        longitude=_decimal(lon, 6),
        speed=_decimal(row.get("speed"), 2),
        heading=heading,
        ignition=None if row.get("ignition") is None else bool(row["ignition"]),
        odometer=_decimal(Decimal(str(odometer_m)) / Decimal(1000), 1) if odometer_m is not None else None,
        metadata={
            "source": "comms",
            "comms_id": row.get("id"),
            "gps_status": row.get("gpsstatus"),
            # Teltonika trip odometer (AVL 199), metres -> km; string keeps JSON serializable.
            "gps_odometer_km": (
                None if gps_odometer_m is None
                else str(_decimal(Decimal(str(gps_odometer_m)) / Decimal(1000), 3))
            ),
            "location": row.get("location") or "",
            **({} if row.get("panic") is None else {"panic": int(row["panic"])}),
            **({} if row.get("panic_voltage") is None else {"panic_voltage": row["panic_voltage"]}),
        },
    )


def apply_rows(rows, report, tz=None):
    """Persist a batch of comms rows into FMS. Rows are grouped per unit so
    each device is ingested once per batch (one transaction, one live-position
    update). Returns nothing; results accumulate on ``report``."""
    tz = tz or _local_timezone()
    by_unit = defaultdict(list)
    for row in rows:
        by_unit[str(row["unitno"]).strip()].append(row)

    devices = {
        d.imei: d for d in TrackingDevice.objects.select_related("vehicle").filter(imei__in=list(by_unit))
    }
    for unit, unit_rows in by_unit.items():
        device = devices.get(unit)
        if device is None:
            report.unknown_units[unit] += len(unit_rows)
            continue
        if device.status != TrackingDevice.Status.ACTIVE:
            report.inactive_units[unit] += len(unit_rows)
            continue
        events = [e for e in (row_to_event(r, tz) for r in unit_rows) if e is not None]
        if not events:
            continue
        result = TelemetryIngestionService.ingest_events(device=device, events=events)
        report.accepted += result.accepted
        report.rejected += result.rejected


def attach_panic_voltage(rows, raw_dsn=None):
    """Adds ``panic_voltage`` (volts, as a string) to the panic rows of a batch,
    read from the comms Raw DB when ``COMMS_RAW_DATABASE_URL`` is set. Best
    effort: without it, or on any error, alerts simply show no voltage."""
    raw_dsn = raw_dsn if raw_dsn is not None else settings.COMMS_RAW_DATABASE_URL
    panic_rows = [r for r in rows if r.get("panic") == 1 and r.get("tracktime") is not None and r.get("unitno")]
    if not raw_dsn or not panic_rows:
        return
    keys = [str(r["unitno"]).strip() for r in panic_rows]
    times = [r["tracktime"] for r in panic_rows]
    try:
        with _connect(raw_dsn) as raw:
            found = raw.execute(_PANIC_VOLTAGE_SQL, (keys, times, min(times), max(times))).fetchall()
    except Exception:
        logger.warning("Panic voltage lookup in the comms Raw DB failed", exc_info=True)
        return
    by_key = {(f["unitno"], f["tracktime"]): f["analog1"] for f in found if f["analog1"] is not None}
    for row, unit in zip(panic_rows, keys):
        millivolts = by_key.get((unit, row["tracktime"]))
        if millivolts is not None:
            row["panic_voltage"] = str(_decimal(Decimal(str(millivolts)) / Decimal(1000), 2))


def _connect(dsn):
    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(dsn, connect_timeout=10, row_factory=dict_row)
    conn.read_only = True  # the bridge must never be able to modify comms data
    return conn


def sync_exclusive(**kwargs):
    """``sync()`` guarded by the advisory lock. Returns ``None`` (nothing done)
    when another process is already syncing."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [ADVISORY_LOCK_KEY])
        if not cursor.fetchone()[0]:
            return None
    try:
        return sync(**kwargs)
    finally:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [ADVISORY_LOCK_KEY])
        except Exception:  # connection already gone -> PostgreSQL released the lock with it
            logger.debug("advisory unlock skipped", exc_info=True)


def sync(*, dsn=None, history_days=DEFAULT_HISTORY_DAYS, batch_size=DEFAULT_BATCH_SIZE, max_rows=DEFAULT_MAX_ROWS,
         connection=None):
    """One sync pass: latest position per unit first (so the map is current
    immediately), then history after the saved cursor. Returns a SyncReport.
    ``connection`` lets tests/callers supply an open psycopg connection."""
    dsn = dsn or settings.COMMS_APP_DATABASE_URL
    if connection is None and not dsn:
        raise RuntimeError("COMMS_APP_DATABASE_URL is not configured.")

    state, _ = CommsSyncState.objects.get_or_create(key=STATE_KEY)
    state.last_run_at = timezone.now()
    report = SyncReport(last_history_id=state.last_id)
    own_connection = connection is None
    conn = connection or _connect(dsn)
    tz = _local_timezone()
    try:
        current_rows = conn.execute(f"SELECT {_COLUMNS} FROM current_table").fetchall()
        report.current_rows = len(current_rows)
        attach_panic_voltage(current_rows)
        apply_rows(current_rows, report, tz)

        if state.last_id == 0:
            # First run: start `history_days` back instead of importing years of
            # pre-FMS history. (Use --history-days to change this.)
            cutoff = datetime.datetime.now(tz).replace(tzinfo=None) - datetime.timedelta(days=history_days)
            first = conn.execute("SELECT min(id) AS m FROM history_table WHERE tracktime >= %s", (cutoff,)).fetchone()
            state.last_id = (first["m"] - 1) if first and first["m"] else (
                conn.execute("SELECT coalesce(max(id), 0) AS m FROM history_table").fetchone()["m"]
            )

        remaining = max_rows
        while remaining > 0:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM history_table WHERE id > %s ORDER BY id LIMIT %s",
                (state.last_id, min(batch_size, remaining)),
            ).fetchall()
            if not rows:
                break
            report.history_rows += len(rows)
            attach_panic_voltage(rows)
            apply_rows(rows, report, tz)
            state.last_id = rows[-1]["id"]
            remaining -= len(rows)

        report.last_history_id = state.last_id
        state.last_success_at = timezone.now()
        state.last_error = ""
        state.stats = report.as_dict()
    except Exception as exc:
        state.last_error = f"{type(exc).__name__}: {exc}"[:2000]
        state.save()
        raise
    finally:
        if own_connection:
            conn.close()
    state.save()
    return report


def backfill_panic_flags(*, days, dsn=None, connection=None):
    """For history imported BEFORE the bridge carried ``panic``: copy the comms
    panic flag (and voltage) onto the matching FMS records of the last ``days``
    days, then rebuild those vehicles' alert events WITHOUT notifying anyone
    (they are past events). Idempotent. Returns {"updated", "alerts_created"}."""
    from django.db import transaction

    from apps.alerts.events import process_vehicle_signals
    from apps.tracking.models import TelemetryEvent

    dsn = dsn or settings.COMMS_APP_DATABASE_URL
    if connection is None and not dsn:
        raise RuntimeError("COMMS_APP_DATABASE_URL is not configured.")
    tz = _local_timezone()
    cutoff = datetime.datetime.now(tz).replace(tzinfo=None) - datetime.timedelta(days=days)
    conn = connection or _connect(dsn)
    try:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM history_table WHERE panic IS NOT NULL AND tracktime >= %s ORDER BY tracktime",
            (cutoff,),
        ).fetchall()
    finally:
        if connection is None:
            conn.close()
    attach_panic_voltage(rows)

    by_unit = defaultdict(list)
    for row in rows:
        event = row_to_event(row, tz)
        if event is not None:
            by_unit[str(row["unitno"]).strip()].append(event)
    devices = {d.imei: d for d in TrackingDevice.objects.select_related("vehicle").filter(imei__in=list(by_unit))}

    updated = created = 0
    for unit, events in by_unit.items():
        device = devices.get(unit)
        if device is None or device.vehicle is None:
            continue
        wanted = {(e.timestamp, e.latitude, e.longitude): e.metadata for e in events}
        with transaction.atomic():
            records = list(TelemetryEvent.objects.filter(device=device, timestamp__in=[e.timestamp for e in events]))
            for record in records:
                metadata = wanted.get((record.timestamp, record.latitude, record.longitude))
                if metadata is None:
                    continue
                extra = {k: metadata[k] for k in ("panic", "panic_voltage") if k in metadata}
                if any(record.metadata.get(k) != v for k, v in extra.items()):
                    record.metadata = {**record.metadata, **extra}
                    record.save(update_fields=["metadata"])
                    updated += 1
            created += len(process_vehicle_signals(vehicle=device.vehicle, device=device, readings=records, notify=False))
    return {"updated": updated, "alerts_created": created}
