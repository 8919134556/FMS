"""Device administration + telemetry ingestion business logic.

Kept out of views/API views for the same reason as every other app in this
project (apps.trips.services, apps.maintenance.services): "validate, then
commit atomically, then log" belongs in one obvious place per operation,
not scattered across view methods.
"""

import logging
import secrets
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.core.scoping import scope_queryset
from apps.tracking.models import RawTelemetryEvent, TelemetryEvent, TrackingDevice, VehicleCurrentTelemetry
from apps.tracking.providers import UnsupportedProviderError, get_parser

logger = logging.getLogger(__name__)

MAX_PLAUSIBLE_SPEED_KMH = 300
MAX_FUTURE_SKEW = timezone.timedelta(days=1)


def issue_device_key(*, device, actor, request=None):
    """Generates a new raw secret, stores only its hash, and returns the raw
    value — the ONLY time it is ever available. Used both for a brand-new
    device and for "regenerate key" (silently invalidates the previous one)."""
    raw_secret = secrets.token_urlsafe(32)
    device.secret_hash = make_password(raw_secret)
    device.updated_by = actor
    device.save(update_fields=["secret_hash", "updated_by", "updated_at"])
    log_action(
        action=AuditLog.Action.UPDATE, module="tracking_device", entity="TrackingDevice", entity_id=str(device.pk),
        new_value={"key_issued": True}, user=actor, request=request,
    )
    return raw_secret


@transaction.atomic
def assign_device(*, device, vehicle, actor, request=None):
    from apps.tracking.models import TrackingDevice as _TD

    existing = _TD.objects.filter(vehicle=vehicle).exclude(pk=device.pk).first()
    if existing:
        raise ValidationError(f"{vehicle.registration_number} already has a device assigned ({existing.imei}).")

    device.vehicle = vehicle
    device.updated_by = actor
    device.save(update_fields=["vehicle", "updated_by", "updated_at"])
    log_action(
        action=AuditLog.Action.ASSIGN, module="tracking_device", entity="TrackingDevice", entity_id=str(device.pk),
        new_value={"vehicle_id": vehicle.pk}, user=actor, request=request,
    )
    return device


@transaction.atomic
def unassign_device(*, device, actor, request=None):
    old_vehicle_id = device.vehicle_id
    device.vehicle = None
    device.updated_by = actor
    device.save(update_fields=["vehicle", "updated_by", "updated_at"])
    log_action(
        action=AuditLog.Action.UNASSIGN, module="tracking_device", entity="TrackingDevice", entity_id=str(device.pk),
        old_value={"vehicle_id": old_vehicle_id}, user=actor, request=request,
    )
    return device


@transaction.atomic
def disable_device(*, device, actor, request=None):
    """"Disable" maps onto the existing SUSPENDED status — see
    apps.tracking.models.TrackingDevice.Status for why no separate DISABLED
    value was added."""
    old_status = device.status
    device.status = TrackingDevice.Status.SUSPENDED
    device.updated_by = actor
    device.save(update_fields=["status", "updated_by", "updated_at"])
    log_action(
        action=AuditLog.Action.UPDATE, module="tracking_device", entity="TrackingDevice", entity_id=str(device.pk),
        old_value={"status": old_status}, new_value={"status": device.status}, user=actor, request=request,
    )
    return device


@transaction.atomic
def enable_device(*, device, actor, request=None):
    old_status = device.status
    device.status = TrackingDevice.Status.ACTIVE
    device.updated_by = actor
    device.save(update_fields=["status", "updated_by", "updated_at"])
    log_action(
        action=AuditLog.Action.UPDATE, module="tracking_device", entity="TrackingDevice", entity_id=str(device.pk),
        old_value={"status": old_status}, new_value={"status": device.status}, user=actor, request=request,
    )
    return device


def validate_normalized_event(event):
    """Domain validation (value bounds) — separate from provider parsing
    (shape/format). Returns a list of human-readable problems; empty list
    means valid. Never silently accepts malformed coordinates."""
    problems = []
    if event.latitude is None or not (-90 <= event.latitude <= 90):
        problems.append(f"latitude out of range: {event.latitude}")
    if event.longitude is None or not (-180 <= event.longitude <= 180):
        problems.append(f"longitude out of range: {event.longitude}")
    if event.speed is not None and not (0 <= event.speed <= MAX_PLAUSIBLE_SPEED_KMH):
        problems.append(f"speed out of range: {event.speed}")
    if event.timestamp is None:
        problems.append("missing timestamp")
    else:
        if event.timestamp.tzinfo is None:
            problems.append("timestamp must be timezone-aware")
        elif event.timestamp > timezone.now() + MAX_FUTURE_SKEW:
            problems.append(f"timestamp too far in the future: {event.timestamp}")
    return problems


@transaction.atomic
def _metadata_decimal(metadata, key):
    """Optional numeric extra carried in ``NormalizedEvent.metadata`` (kept as
    a string there so the history JSON column stays serializable)."""
    value = (metadata or {}).get(key)
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def upsert_current_telemetry(*, vehicle, device, event):
    """Out-of-order-safe, race-safe current-position upsert.

    ``select_for_update()`` mirrors the row-lock idiom already used by
    apps.trips.services/apps.maintenance.services for their number-sequence
    singletons — here it makes the read-check-write atomic across
    concurrent ingestion requests for the same vehicle. An incoming event
    older-or-equal to the stored timestamp is a deliberate no-op on current
    state (it was already written to TelemetryEvent history by the caller).
    """
    defaults = {
        "device": device,
        "timestamp": event.timestamp,
        "latitude": event.latitude,
        "longitude": event.longitude,
        "speed": event.speed,
        "heading": event.heading,
        "ignition": event.ignition,
        "odometer": event.odometer,
        "location": ((event.metadata or {}).get("location") or "")[:255],
        "gps_odometer": _metadata_decimal(event.metadata, "gps_odometer_km"),
    }
    obj, created = VehicleCurrentTelemetry.objects.select_for_update().get_or_create(
        vehicle=vehicle, defaults=defaults
    )
    if created:
        return obj
    if event.timestamp > obj.timestamp:
        from apps.tracking.trip_report import _has_gps_fix

        fields = dict(defaults)
        if not _has_gps_fix(event.latitude, event.longitude) and _has_gps_fix(obj.latitude, obj.longitude):
            # A (0,0) "no GPS fix yet" reading (sent right after ignition-on,
            # before the device locks on) is newer but carries no position:
            # keep the last known GOOD position and its address — the same rule
            # trip_report._apply_live_position applies — while time, speed,
            # ignition and odometer still advance. The reading itself is kept
            # in TelemetryEvent history by the caller.
            for field_name in ("latitude", "longitude", "location"):
                fields.pop(field_name)
        for field_name, value in fields.items():
            setattr(obj, field_name, value)
        obj.save(update_fields=list(fields.keys()) + ["updated_at"])
    elif event.timestamp == obj.timestamp:
        # Same reading re-delivered with extra detail (e.g. the row was stored
        # before addresses / GPS odometer were carried over): fill the gaps,
        # change nothing else.
        gaps = [
            name for name in ("location", "gps_odometer")
            if defaults[name] not in (None, "") and getattr(obj, name) in (None, "")
        ]
        for name in gaps:
            setattr(obj, name, defaults[name])
        if gaps:
            obj.save(update_fields=gaps + ["updated_at"])
    return obj


def connection_status_for(current):
    """ONLINE / STALE / OFFLINE, computed live from
    ``settings.TELEMATICS_ONLINE_THRESHOLD_MINUTES`` /
    ``TELEMATICS_OFFLINE_THRESHOLD_MINUTES`` — never stored, so it can never
    go stale itself. ``None`` (no current telemetry at all) is OFFLINE."""
    if current is None:
        return "OFFLINE"
    age = timezone.now() - current.timestamp
    if age <= timezone.timedelta(minutes=settings.TELEMATICS_ONLINE_THRESHOLD_MINUTES):
        return "ONLINE"
    if age <= timezone.timedelta(minutes=settings.TELEMATICS_OFFLINE_THRESHOLD_MINUTES):
        return "STALE"
    return "OFFLINE"


def _evaluate_geofences(*, vehicle, event):
    """Best-effort geofence breach check on every position update. Isolated
    behind a try/except deliberately: geofencing is additive functionality,
    and a bug or bad geofence row must never break telemetry ingestion
    itself (the one thing this whole app exists to do reliably)."""
    try:
        from apps.geofences.services import evaluate_position

        evaluate_position(vehicle, event.latitude, event.longitude, occurred_at=event.timestamp)
    except Exception:
        logger.exception("Geofence evaluation failed for vehicle %s", vehicle.pk)


def _evaluate_alerts(*, vehicle, device, events):
    """Vehicle alert events (panic, idle) from the readings just stored —
    apps.alerts.events is the single detector; it isolates its own failures."""
    from apps.alerts.events import evaluate_ingested

    evaluate_ingested(vehicle=vehicle, device=device, readings=events)


def _persist_events(*, device, vehicle, client_id, events, parse_errors, raw_event):
    """Validate -> store history -> advance the live position -> geofences ->
    alert events.
    Shared by socket/HTTP ingest (``raw_event`` present) and the comms bridge
    (``raw_event`` None). Must run inside the caller's transaction."""
    valid_events = []
    all_errors = list(parse_errors)
    for index, event in enumerate(events):
        problems = validate_normalized_event(event)
        if problems:
            all_errors.append({"index": index, "error": "; ".join(problems)})
        else:
            valid_events.append(event)

    if valid_events:
        TelemetryEvent.objects.bulk_create(
            [
                TelemetryEvent(
                    device=device,
                    vehicle=vehicle,
                    client_id=client_id,
                    timestamp=ev.timestamp,
                    latitude=ev.latitude,
                    longitude=ev.longitude,
                    speed=ev.speed,
                    heading=ev.heading,
                    altitude=ev.altitude,
                    ignition=ev.ignition,
                    odometer=ev.odometer,
                    engine_hours=ev.engine_hours,
                    battery_voltage=ev.battery_voltage,
                    external_power=ev.external_power,
                    signal_strength=ev.signal_strength,
                    satellite_count=ev.satellite_count,
                    metadata=ev.metadata,
                )
                for ev in valid_events
            ],
            ignore_conflicts=True,
        )

        if vehicle is not None:
            latest_event = max(valid_events, key=lambda ev: ev.timestamp)
            upsert_current_telemetry(vehicle=vehicle, device=device, event=latest_event)
            _evaluate_geofences(vehicle=vehicle, event=latest_event)
            _evaluate_alerts(vehicle=vehicle, device=device, events=valid_events)

    if raw_event is not None:
        device.last_communication = timezone.now()
    elif valid_events:
        # Bridge data: "last communication" is the device's own last report,
        # never later than a value we already have.
        latest_ts = max(ev.timestamp for ev in valid_events)
        if device.last_communication is None or latest_ts > device.last_communication:
            device.last_communication = latest_ts
    device.save(update_fields=["last_communication", "updated_at"])

    if raw_event is not None:
        if not all_errors:
            raw_event.processing_status = RawTelemetryEvent.ProcessingStatus.PROCESSED
        elif valid_events:
            raw_event.processing_status = RawTelemetryEvent.ProcessingStatus.PARTIAL
        else:
            raw_event.processing_status = RawTelemetryEvent.ProcessingStatus.FAILED
        raw_event.error_message = "; ".join(f"[{e.get('index', '-')}] {e['error']}" for e in all_errors)[:2000]
        raw_event.save(update_fields=["processing_status", "error_message"])

    return IngestionResult(accepted=len(valid_events), rejected=len(all_errors), errors=all_errors)


@dataclass
class IngestionResult:
    accepted: int
    rejected: int
    errors: list = field(default_factory=list)


class TelemetryIngestionService:
    """Orchestrates one ingestion request (single event or batch) end to
    end. The only entry point views should call — keeps views thin."""

    @staticmethod
    @transaction.atomic
    def ingest(*, device, raw_payload):
        # Ownership is resolved from the database on EVERY packet, never from
        # the ``device`` object the caller holds: the TCP server keeps one
        # TrackingDevice per open connection for hours, and a device that was
        # re-assigned to another vehicle/client (or suspended) in the
        # meantime must not keep feeding its previous owner.
        device = TrackingDevice.objects.select_related("vehicle").get(pk=device.pk)
        vehicle = device.vehicle
        client_id = vehicle.client_id if vehicle is not None else None

        raw_event = RawTelemetryEvent.objects.create(
            device=device, vehicle=vehicle, client_id=client_id, provider=device.provider, payload=raw_payload
        )

        if device.status != TrackingDevice.Status.ACTIVE:
            raw_event.processing_status = RawTelemetryEvent.ProcessingStatus.FAILED
            raw_event.error_message = f"Device is {device.status}, telemetry not processed."
            raw_event.save(update_fields=["processing_status", "error_message"])
            return IngestionResult(accepted=0, rejected=1, errors=[{"error": raw_event.error_message}])

        try:
            parser = get_parser(device.provider)
        except UnsupportedProviderError as exc:
            raw_event.processing_status = RawTelemetryEvent.ProcessingStatus.FAILED
            raw_event.error_message = str(exc)
            raw_event.save(update_fields=["processing_status", "error_message"])
            return IngestionResult(accepted=0, rejected=1, errors=[{"error": str(exc)}])

        parsed_events, parse_errors = parser.parse(raw_payload)
        return _persist_events(
            device=device, vehicle=vehicle, client_id=client_id, events=parsed_events,
            parse_errors=parse_errors, raw_event=raw_event,
        )

    @staticmethod
    @transaction.atomic
    def ingest_events(*, device, events):
        """Same pipeline as ``ingest`` for events that are ALREADY normalized
        (no wire payload to parse) — used by the comms bridge
        (apps.tracking.comms_sync). Ownership and ACTIVE status are re-read
        from the database exactly as ``ingest`` does, and validation, dedup,
        client stamping, the live-position upsert and geofence evaluation are
        the very same code. No RawTelemetryEvent is written: the raw archive
        for this source is the comms Raw DB.
        """
        device = TrackingDevice.objects.select_related("vehicle").get(pk=device.pk)
        if device.status != TrackingDevice.Status.ACTIVE:
            return IngestionResult(accepted=0, rejected=len(events), errors=[{"error": f"Device is {device.status}."}])
        vehicle = device.vehicle
        return _persist_events(
            device=device, vehicle=vehicle, client_id=vehicle.client_id if vehicle is not None else None,
            events=events, parse_errors=[], raw_event=None,
        )


# ---------------------------------------------------------------------------
# Phase 3.4 — Live Fleet Map read helpers. All of these are pure
# read/aggregation, no mutation — RBAC is the caller's job (view-level
# permission_classes), same split as connection_status_for.
# ---------------------------------------------------------------------------

def movement_state_for(current):
    """MOVING/IDLE from ``current.speed`` vs
    ``settings.TELEMATICS_MOVEMENT_SPEED_THRESHOLD_KMH`` — the one place
    this threshold is applied, so no frontend file hardcodes it. ``None``
    when there's no telemetry or speed is unknown; never guessed."""
    if current is None or current.speed is None:
        return None
    return "MOVING" if current.speed > settings.TELEMATICS_MOVEMENT_SPEED_THRESHOLD_KMH else "IDLE"


def fleet_current_telemetry(user=None):
    """Everything the live map needs, in two queries total regardless of
    fleet size — no per-vehicle N+1. Only vehicles with a
    VehicleCurrentTelemetry row are included (never a fabricated position).

    ``user`` scopes the result: a client user only ever gets their own
    client's vehicles (apps.core.scoping); staff/None get the whole fleet."""
    from apps.trips.models import Trip

    current_rows = list(
        scope_queryset(
            VehicleCurrentTelemetry.objects.select_related(
                "vehicle", "vehicle__vehicle_type", "vehicle__client", "vehicle__current_driver", "device"
            ),
            user, "vehicle__client_id",
        ).order_by("vehicle__registration_number")
    )
    vehicle_ids = [row.vehicle_id for row in current_rows]
    active_trips_by_vehicle = {
        row["vehicle_id"]: {"uuid": str(row["uuid"]), "trip_number": row["trip_number"]}
        for row in Trip.objects.filter(
            vehicle_id__in=vehicle_ids, status__in=Trip.ACTIVE_ASSIGNMENT_STATUSES
        ).values("vehicle_id", "uuid", "trip_number")
    }

    results = []
    for row in current_rows:
        vehicle = row.vehicle
        results.append(
            {
                "vehicle_uuid": vehicle.uuid,
                "registration_number": vehicle.registration_number,
                "availability_status": vehicle.availability_status,
                "vehicle_type": vehicle.vehicle_type.name if vehicle.vehicle_type_id else "",
                "client": vehicle.client.client_name if vehicle.client_id else "",
                "driver_name": vehicle.current_driver.get_full_name() if vehicle.current_driver_id else None,
                "latitude": row.latitude,
                "longitude": row.longitude,
                "speed": row.speed,
                "heading": row.heading,
                "ignition": row.ignition,
                "odometer": row.odometer,
                "location": row.location,
                "gps_odometer": row.gps_odometer,
                "timestamp": row.timestamp,
                "connection_status": connection_status_for(row),
                "movement_state": movement_state_for(row),
                "active_trip": active_trips_by_vehicle.get(row.vehicle_id),
            }
        )
    return results


COMMS_SYNC_HEALTHY_SECONDS = 120  # the bridge loop runs every ~10 s; 2 min of silence means it is down


def comms_sync_status():
    """Is the comms -> FMS bridge (``manage.py sync_comms_data --loop``) alive?

    ``None`` when no comms database is configured (devices report straight to
    FMS, so there is nothing to monitor). Otherwise a small, non-sensitive
    dict — never the raw error text — the Live Tracking page uses to warn that
    positions may be stale instead of silently showing old data."""
    if not settings.COMMS_APP_DATABASE_URL:
        return None
    from apps.tracking.comms_sync import STATE_KEY
    from apps.tracking.models import CommsSyncState

    state = CommsSyncState.objects.filter(key=STATE_KEY).only("last_success_at", "last_error").first()
    last_success = state.last_success_at if state else None
    age = None if last_success is None else max(0, int((timezone.now() - last_success).total_seconds()))
    return {
        "enabled": True,
        "last_success_at": last_success.isoformat() if last_success else None,
        "age_seconds": age,
        "healthy": age is not None and age <= COMMS_SYNC_HEALTHY_SECONDS,
        "has_error": bool(state and state.last_error),
    }


def fleet_connectivity_counts(user=None):
    """Dashboard widget counts — computed with the exact same threshold
    cutoffs ``connection_status_for`` uses (expressed as SQL WHERE, not a
    per-row Python loop, so it stays a handful of COUNT queries regardless
    of fleet size). ``no_telemetry`` = any vehicle without a
    VehicleCurrentTelemetry row at all, matching the Vehicle Detail GPS
    tab's own "No telemetry received" empty-state condition exactly."""
    from apps.vehicles.models import Vehicle

    now = timezone.now()
    online_cutoff = now - timezone.timedelta(minutes=settings.TELEMATICS_ONLINE_THRESHOLD_MINUTES)
    offline_cutoff = now - timezone.timedelta(minutes=settings.TELEMATICS_OFFLINE_THRESHOLD_MINUTES)

    total_vehicles = scope_queryset(Vehicle.objects.all(), user).count()
    current_qs = scope_queryset(VehicleCurrentTelemetry.objects.all(), user, "vehicle__client_id")
    total_tracked = current_qs.count()
    online = current_qs.filter(timestamp__gte=online_cutoff).count()
    offline = current_qs.filter(timestamp__lt=offline_cutoff).count()
    stale = total_tracked - online - offline

    return {
        "total_tracked": total_tracked,
        "online": online,
        "stale": stale,
        "offline": offline,
        "no_telemetry": total_vehicles - total_tracked,
    }
