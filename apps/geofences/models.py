from django.db import models

from apps.core.models import BaseFleetModel, TimeStampedModel


class Geofence(BaseFleetModel):
    """A circular boundary the fleet is monitored against.

    Deliberately circle-only (center + radius), not arbitrary polygons —
    there's no map-drawing UI in this build to author a polygon with, and a
    circle covers the real use cases (client site radius, branch/depot
    radius, a delivery-zone radius) without needing one. A Geofence may
    optionally reference a Site or Branch purely for context/pre-filling —
    it does not have to (a geofence can stand alone, e.g. a restricted zone).
    """

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)

    center_latitude = models.DecimalField(max_digits=9, decimal_places=6)
    center_longitude = models.DecimalField(max_digits=9, decimal_places=6)
    radius_meters = models.PositiveIntegerField(help_text="Radius in meters, minimum 50.")

    site = models.ForeignKey(
        "locations.Site", null=True, blank=True, on_delete=models.SET_NULL, related_name="geofences"
    )
    branch = models.ForeignKey(
        "locations.Branch", null=True, blank=True, on_delete=models.SET_NULL, related_name="geofences"
    )

    notify_on_enter = models.BooleanField(default=True)
    notify_on_exit = models.BooleanField(default=True)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.ACTIVE, db_index=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["code"]), models.Index(fields=["status"])]

    def __str__(self):
        return self.name


class GeofenceEvent(TimeStampedModel):
    """One ENTER/EXIT crossing, recorded by apps.geofences.services.evaluate_position
    whenever a vehicle's live position updates (see apps.tracking.services.
    TelemetryIngestionService.ingest). No soft-delete/UUID — this is an
    immutable log entry, never edited, and never linked to from a
    shareable URL."""

    class EventType(models.TextChoices):
        ENTER = "ENTER", "Entered"
        EXIT = "EXIT", "Exited"

    geofence = models.ForeignKey(Geofence, on_delete=models.CASCADE, related_name="events")
    vehicle = models.ForeignKey("vehicles.Vehicle", on_delete=models.CASCADE, related_name="geofence_events")
    event_type = models.CharField(max_length=10, choices=EventType.choices)
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    occurred_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [models.Index(fields=["geofence", "vehicle", "-occurred_at"])]

    def __str__(self):
        return f"{self.vehicle} {self.get_event_type_display()} {self.geofence} @ {self.occurred_at}"
