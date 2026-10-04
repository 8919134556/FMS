from django.conf import settings
from django.db import models

from apps.core.models import BaseFleetModel, TimeStampedModel, UUIDModel


class TrackingDevice(BaseFleetModel):
    """GPS/telematics device master.

    Extended in Phase 3.3 with the fields needed for device-authenticated
    telemetry ingestion (provider, hashed secret, firmware) — the original
    identification/status fields are untouched so existing consumers
    (dashboard GPS counts, Vehicle list `?gps=` filter, Vehicle Detail GPS
    tab) keep working unchanged.
    """

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"
        FAULTY = "FAULTY", "Faulty"
        REMOVED = "REMOVED", "Removed"
        SUSPENDED = "SUSPENDED", "Suspended"

    class Provider(models.TextChoices):
        GENERIC = "generic", "Generic JSON"
        TELTONIKA = "teltonika", "Teltonika"
        MDVR = "mdvr", "MDVR"
        NAVTELECOM = "navtelecom", "NavTelecom"
        OTHER = "other", "Other"

    imei = models.CharField(max_length=20, unique=True)
    serial_number = models.CharField(max_length=60, blank=True)
    device_model = models.CharField(max_length=100, blank=True)
    manufacturer = models.CharField(max_length=100, blank=True)
    sim_number = models.CharField(max_length=20, blank=True)
    sim_provider = models.CharField(max_length=60, blank=True)
    vehicle = models.OneToOneField(
        "vehicles.Vehicle", null=True, blank=True, on_delete=models.SET_NULL, related_name="tracking_device"
    )
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    last_communication = models.DateTimeField(null=True, blank=True)

    name = models.CharField(max_length=120, blank=True, default="")
    provider = models.CharField(max_length=20, choices=Provider.choices, default=Provider.GENERIC, db_index=True)
    # Hash of the device's ingestion secret (django.contrib.auth.hashers) —
    # the raw secret is shown once at issue/regenerate time and never stored
    # anywhere else. Never exposed via any serializer/template.
    secret_hash = models.CharField(max_length=255, blank=True, default="")
    firmware_version = models.CharField(max_length=50, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["imei"]), models.Index(fields=["status"])]

    def __str__(self):
        return self.imei


class RawTelemetryEvent(models.Model):
    """Original incoming payload, kept for debugging/parser troubleshooting.

    Plain ``models.Model`` (not ``BaseFleetModel``) — this is a high-volume,
    append-only, never-user-edited table, so soft-delete/audit-user FKs
    would be pure overhead. Retention is a documented concern
    (``settings.TELEMATICS_RAW_EVENT_RETENTION_DAYS``), not an enforced job
    in this phase.
    """

    class ProcessingStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSED = "PROCESSED", "Processed"
        PARTIAL = "PARTIAL", "Partially Processed"
        FAILED = "FAILED", "Failed"

    device = models.ForeignKey(TrackingDevice, on_delete=models.PROTECT, related_name="raw_events")
    # Ownership snapshot taken at receive time (device -> vehicle -> client),
    # so a raw packet stays attributable to the client who owned the vehicle
    # when it arrived even if the device/vehicle is re-assigned later. NULL
    # means the device wasn't mapped to a vehicle (or the vehicle to a
    # client) yet — such rows are visible to internal staff only.
    vehicle = models.ForeignKey(
        "vehicles.Vehicle", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    client = models.ForeignKey(
        "clients.Client", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    provider = models.CharField(max_length=20, help_text="Snapshot of device.provider at ingest time.")
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    payload = models.JSONField()
    processing_status = models.CharField(
        max_length=10, choices=ProcessingStatus.choices, default=ProcessingStatus.PENDING, db_index=True
    )
    error_message = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-received_at"]
        indexes = [
            models.Index(fields=["device", "received_at"]),
            models.Index(fields=["client", "received_at"]),
        ]

    def __str__(self):
        return f"RawTelemetryEvent(device={self.device_id}, received_at={self.received_at})"


class TelemetryEvent(models.Model):
    """Normalized telemetry history — one row per GPS/telematics reading.

    Latitude/longitude are ``DecimalField`` (not ``FloatField``) deliberately:
    the dedup uniqueness constraint below needs exact equality, which float
    comparison can't guarantee.
    """

    device = models.ForeignKey(TrackingDevice, on_delete=models.PROTECT, related_name="telemetry_events")
    vehicle = models.ForeignKey(
        "vehicles.Vehicle", null=True, blank=True, on_delete=models.SET_NULL, related_name="telemetry_events"
    )
    # Denormalized on purpose: the client who owned the vehicle when this
    # reading was ingested. History must NOT follow a vehicle to its next
    # client, and per-client reads become one indexed filter instead of a
    # join through vehicles — which is what keeps this table queryable with
    # thousands of clients/vehicles. NULL = unmapped (staff-only).
    client = models.ForeignKey(
        "clients.Client", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    timestamp = models.DateTimeField(db_index=True, help_text="Device/event time (distinct from received_at).")
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    speed = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True, help_text="km/h")
    heading = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Degrees, 0-359.")
    altitude = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True, help_text="Metres.")
    ignition = models.BooleanField(null=True, blank=True)
    odometer = models.DecimalField(max_digits=10, decimal_places=1, null=True, blank=True)
    engine_hours = models.DecimalField(max_digits=8, decimal_places=1, null=True, blank=True)
    battery_voltage = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    external_power = models.BooleanField(null=True, blank=True)
    signal_strength = models.PositiveSmallIntegerField(null=True, blank=True)
    satellite_count = models.PositiveSmallIntegerField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True, help_text="Provider-specific extra fields.")

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["device", "timestamp"]),
            models.Index(fields=["vehicle", "timestamp"]),
            models.Index(fields=["client", "timestamp"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["device", "timestamp", "latitude", "longitude"], name="uniq_telemetry_device_ts_lat_lon"
            ),
        ]

    def __str__(self):
        return f"TelemetryEvent(device={self.device_id}, timestamp={self.timestamp})"


class VehicleCurrentTelemetry(TimeStampedModel):
    """Latest-known position/state per vehicle — updated in place so reading
    "where is this vehicle now" never means scanning the history table.

    ``timestamp`` only ever moves forward (see
    apps.tracking.services.upsert_current_telemetry) — an older, late-
    arriving event is still written to ``TelemetryEvent`` history but never
    regresses this row. ``updated_at`` (inherited) is "Last Updated".
    """

    vehicle = models.OneToOneField(
        "vehicles.Vehicle", on_delete=models.CASCADE, related_name="current_telemetry"
    )
    device = models.ForeignKey(
        TrackingDevice, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    timestamp = models.DateTimeField()
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    speed = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    heading = models.PositiveSmallIntegerField(null=True, blank=True)
    ignition = models.BooleanField(null=True, blank=True)
    odometer = models.DecimalField(max_digits=10, decimal_places=1, null=True, blank=True)
    location = models.CharField(
        max_length=255, blank=True, default="",
        help_text="Human-readable address of the latest position (from the comms current_table, when provided).",
    )
    gps_odometer = models.DecimalField(
        max_digits=12, decimal_places=3, null=True, blank=True,
        help_text="GPS trip odometer in km (comms current_table.gpsodometer, reported in metres).",
    )

    class Meta:
        verbose_name_plural = "Vehicle current telemetry"

    def __str__(self):
        return f"CurrentTelemetry(vehicle={self.vehicle_id}, timestamp={self.timestamp})"


class CommsSyncState(models.Model):
    """Cursor + last-run stats for the comms bridge (apps.tracking.comms_sync).

    ``last_id`` is the highest comms App DB ``history_table.id`` already
    imported, so every run continues where the previous one stopped and a
    restart never re-reads (or skips) history."""

    key = models.CharField(max_length=50, unique=True)
    last_id = models.BigIntegerField(default=0)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    stats = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"CommsSyncState({self.key}, last_id={self.last_id})"


def _default_max_retries():
    """A callable default (not a bare int) so a test's ``settings.TELEMATICS_
    COMMAND_DEFAULT_MAX_RETRIES`` override is honored at save-time, not
    frozen at import-time."""
    return settings.TELEMATICS_COMMAND_DEFAULT_MAX_RETRIES


class DeviceCommand(UUIDModel, TimeStampedModel):
    """A command queued for delivery to a device via the telematics TCP
    service (apps.tracking.telematics_service) — Phase 3.5.

    Not ``BaseFleetModel``: soft-delete doesn't fit a state-machine audit
    row, and a single ``created_by`` FK is enough (human-issued only; None
    for system-driven transitions like an ACK-triggered COMPLETED).

    ``completed_at`` is stamped on ANY terminal transition (COMPLETED,
    FAILED, EXPIRED, CANCELLED) — "reached a terminal state", not
    "succeeded only". All status mutation goes through
    ``apps.tracking.command_services.transition_command`` — never assign
    ``.status`` directly elsewhere.
    """

    class CommandType(models.TextChoices):
        PING = "PING", "Ping"
        REQUEST_LOCATION = "REQUEST_LOCATION", "Request Location"
        REBOOT = "REBOOT", "Reboot"
        SET_REPORTING_INTERVAL = "SET_REPORTING_INTERVAL", "Set Reporting Interval"
        CUSTOM = "CUSTOM", "Custom"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        QUEUED = "QUEUED", "Queued"
        SENT = "SENT", "Sent"
        ACKNOWLEDGED = "ACKNOWLEDGED", "Acknowledged"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"
        EXPIRED = "EXPIRED", "Expired"
        CANCELLED = "CANCELLED", "Cancelled"

    # Explicit transition table — mirrors apps.trips.models.Trip's
    # VALID_TRANSITIONS/can_transition_to pattern. QUEUED->PENDING and
    # SENT->PENDING are safe reverts (claimed-but-not-sent / ACK-timeout
    # retry), not failures. FAILED/EXPIRED->PENDING is the manual/automatic
    # RETRY path (apps.tracking.command_services.retry_command and the
    # ACK-timeout sweep) — bounded by retry_count < max_retries at the
    # service layer, not by this table. PENDING/QUEUED->EXPIRED is the
    # separate expires_at deadline sweep (a command that never got sent in
    # time), independent of retry_count.
    VALID_TRANSITIONS = {
        Status.PENDING: {Status.QUEUED, Status.CANCELLED, Status.EXPIRED},
        Status.QUEUED: {Status.SENT, Status.PENDING, Status.FAILED, Status.CANCELLED, Status.EXPIRED},
        Status.SENT: {Status.ACKNOWLEDGED, Status.PENDING, Status.EXPIRED, Status.FAILED},
        Status.ACKNOWLEDGED: {Status.COMPLETED, Status.FAILED},
        Status.COMPLETED: set(),
        Status.FAILED: {Status.PENDING},
        Status.EXPIRED: {Status.PENDING},
        Status.CANCELLED: set(),
    }

    device = models.ForeignKey(TrackingDevice, on_delete=models.PROTECT, related_name="commands")
    command_type = models.CharField(max_length=30, choices=CommandType.choices)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.PENDING, db_index=True)

    queued_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    # Optional human-set deadline for a command still stuck in
    # PENDING/QUEUED (device never connected in time) — a SEPARATE
    # mechanism from the ACK-timeout retry below, which acts on sent_at.
    expires_at = models.DateTimeField(null=True, blank=True)

    retry_count = models.PositiveSmallIntegerField(default=0)
    max_retries = models.PositiveSmallIntegerField(default=_default_max_retries)
    last_error = models.TextField(blank=True, default="")
    response_payload = models.JSONField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["device", "status"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["status", "expires_at"]),
        ]

    def can_transition_to(self, new_status):
        return new_status in self.VALID_TRANSITIONS.get(self.status, set())

    def __str__(self):
        return f"DeviceCommand({self.command_type}, device={self.device_id}, status={self.status})"
