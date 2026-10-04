from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import BaseFleetModel


class Route(BaseFleetModel):
    """A reusable, named multi-stop itinerary — an ordered sequence of Sites
    a Trip can optionally follow (see Trip.route, added additively; a Trip
    with no route still works exactly as before, using its plain
    origin_site/destination_site pair).

    Deliberately no distance/duration *optimization* engine — there's no
    background worker in this build to run one, and estimated_distance_km/
    estimated_duration_minutes are plain operator-entered fields, the same
    trust level Trip.planned_distance already uses.
    """

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=150)
    client = models.ForeignKey(
        "clients.Client", null=True, blank=True, on_delete=models.SET_NULL, related_name="routes",
        help_text="Optional — leave blank for a route usable by any client.",
    )
    description = models.TextField(blank=True)
    estimated_distance_km = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    estimated_duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.ACTIVE, db_index=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["code"]), models.Index(fields=["status"])]

    def __str__(self):
        return self.name

    @property
    def stop_count(self):
        return self.stops.count()


class RouteStop(models.Model):
    """One ordered waypoint on a Route. Plain model (no BaseFleetModel) —
    a stop has no independent lifecycle; it lives and dies with its Route
    and is never referenced from a URL."""

    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name="stops")
    site = models.ForeignKey("locations.Site", on_delete=models.PROTECT, related_name="route_stops")
    sequence = models.PositiveSmallIntegerField()
    notes = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(fields=["route", "sequence"], name="uniq_route_stop_sequence"),
        ]

    def __str__(self):
        return f"{self.route.name} stop {self.sequence}: {self.site.site_name}"

    def clean(self):
        if self.route_id and self.site_id and self.route.client_id and self.site.client_id != self.route.client_id:
            raise ValidationError({"site": "Site must belong to the route's client."})
