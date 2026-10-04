from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.core.models import BaseFleetModel, TimeStampedModel

MAX_SPEED_LIMIT_KMH = 300


class Geofence(BaseFleetModel):
    """A boundary (circle or polygon) that its ASSIGNED vehicles are monitored
    against, of one of three types (apps.geofences.services):

        ENTRY        alert when an assigned vehicle enters it
        EXIT         alert when an assigned vehicle leaves it
        SPEED_LIMIT  while an assigned vehicle is inside, alert when its speed
                     goes above ``speed_limit_kmh``
        ENTRY_AND_EXIT  both: a Geofence Entry alert on entering AND a
                     Geofence Exit alert on leaving — one geofence, one shape

    Shapes: a CIRCLE is ``center`` + ``radius_meters``; a POLYGON is
    ``polygon`` ([[lat, lon], ...], drawn on the form's map) — its center /
    radius are kept as the enclosing circle, used for the quick distance
    pre-check and by older map overlays. A Geofence may optionally reference a
    Site or Branch purely for context."""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"

    class Shape(models.TextChoices):
        CIRCLE = "CIRCLE", "Circle"
        POLYGON = "POLYGON", "Polygon"

    class GeofenceType(models.TextChoices):
        ENTRY = "ENTRY", "Entry"
        EXIT = "EXIT", "Exit"
        SPEED_LIMIT = "SPEED_LIMIT", "Speed Limit"
        ENTRY_AND_EXIT = "ENTRY_AND_EXIT", "Entry and Exit"

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)

    geofence_type = models.CharField(
        "Geofence type", max_length=15, choices=GeofenceType.choices, default=GeofenceType.ENTRY, db_index=True
    )
    speed_limit_kmh = models.PositiveSmallIntegerField(
        "Speed limit (km/h)", null=True, blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(MAX_SPEED_LIMIT_KMH)],
        help_text="Speed Limit geofences only: alert when an assigned vehicle inside goes faster than this.",
    )
    vehicles = models.ManyToManyField(
        "vehicles.Vehicle", blank=True, related_name="geofences",
        help_text="Only assigned vehicles are monitored against this geofence.",
    )

    shape = models.CharField(max_length=10, choices=Shape.choices, default=Shape.CIRCLE)
    center_latitude = models.DecimalField(max_digits=9, decimal_places=6)
    center_longitude = models.DecimalField(max_digits=9, decimal_places=6)
    radius_meters = models.PositiveIntegerField(help_text="Radius in meters, minimum 50 (polygons: enclosing radius).")
    polygon = models.JSONField(default=list, blank=True, help_text="Polygon vertices [[lat, lon], ...] (polygons only).")

    site = models.ForeignKey(
        "locations.Site", null=True, blank=True, on_delete=models.SET_NULL, related_name="geofences"
    )
    branch = models.ForeignKey(
        "locations.Branch", null=True, blank=True, on_delete=models.SET_NULL, related_name="geofences"
    )

    status = models.CharField(max_length=15, choices=Status.choices, default=Status.ACTIVE, db_index=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["code"]), models.Index(fields=["status"])]

    def __str__(self):
        return self.name

    @property
    def alerts_on_entry(self):
        return self.geofence_type in (self.GeofenceType.ENTRY, self.GeofenceType.ENTRY_AND_EXIT)

    @property
    def alerts_on_exit(self):
        return self.geofence_type in (self.GeofenceType.EXIT, self.GeofenceType.ENTRY_AND_EXIT)

    @property
    def type_summary(self):
        if self.geofence_type == self.GeofenceType.SPEED_LIMIT and self.speed_limit_kmh:
            return f"Speed Limit · {self.speed_limit_kmh} km/h"
        return self.get_geofence_type_display()


class GeofenceEvent(TimeStampedModel):
    """One ENTER/EXIT crossing of an assigned vehicle, recorded by
    apps.geofences.services for every geofence type. The latest event per
    (geofence, vehicle) IS the INSIDE/OUTSIDE state. No soft-delete/UUID — an
    immutable log entry, never edited."""

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
