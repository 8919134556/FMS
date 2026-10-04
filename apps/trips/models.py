from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import BaseFleetModel


class TripNumberSequence(models.Model):
    """Singleton row whose value is incremented under a row lock to hand out
    collision-safe trip numbers (TRP-000001, TRP-000002, ...).

    A plain "count existing trips + 1" scheme breaks under concurrent
    creates and after any trip is deleted/archived; ``select_for_update()``
    here serializes concurrent number generation the same way
    apps.vehicles.services serializes primary-driver reassignment.
    """

    id = models.SmallIntegerField(primary_key=True, default=1)
    last_value = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"TripNumberSequence(last_value={self.last_value})"


class Trip(BaseFleetModel):
    """A single operational movement: a vehicle+driver moving a client's
    goods/people from an origin Site to a destination Site on a schedule.

    Multi-stop routing is intentionally not modeled yet (would need an
    ordered TripStop model + reordering UI); ``instructions`` covers stop
    notes for now and a dedicated stops model can be added additively.
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SCHEDULED = "SCHEDULED", "Scheduled"
        ASSIGNED = "ASSIGNED", "Assigned"
        DISPATCHED = "DISPATCHED", "Dispatched"
        IN_PROGRESS = "IN_PROGRESS", "In Progress"
        DELAYED = "DELAYED", "Delayed"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    # Statuses where the trip has a committed claim on its vehicle/driver —
    # used by the overlap-conflict check. SCHEDULED is excluded: a vehicle
    # merely pencilled onto a not-yet-assigned trip hasn't been committed.
    ACTIVE_ASSIGNMENT_STATUSES = [Status.ASSIGNED, Status.DISPATCHED, Status.IN_PROGRESS, Status.DELAYED]

    # Explicit transition table — see apps.trips.services for the functions
    # that enforce this. Anything not listed as a value is not reachable
    # from that state (e.g. Completed/Cancelled are terminal; Completed ->
    # In Progress and Cancelled -> Scheduled are deliberately impossible).
    VALID_TRANSITIONS = {
        Status.DRAFT: {Status.SCHEDULED, Status.CANCELLED},
        Status.SCHEDULED: {Status.ASSIGNED, Status.CANCELLED},
        Status.ASSIGNED: {Status.DISPATCHED, Status.CANCELLED},
        Status.DISPATCHED: {Status.IN_PROGRESS, Status.CANCELLED},
        Status.IN_PROGRESS: {Status.COMPLETED, Status.DELAYED},
        Status.DELAYED: {Status.IN_PROGRESS, Status.COMPLETED},
        Status.COMPLETED: set(),
        Status.CANCELLED: set(),
    }

    class TripType(models.TextChoices):
        DELIVERY = "DELIVERY", "Delivery"
        PICKUP = "PICKUP", "Pickup"
        TRANSFER = "TRANSFER", "Transfer"
        ROUND_TRIP = "ROUND_TRIP", "Round Trip"
        OTHER = "OTHER", "Other"

    class Priority(models.TextChoices):
        LOW = "LOW", "Low"
        NORMAL = "NORMAL", "Normal"
        HIGH = "HIGH", "High"
        URGENT = "URGENT", "Urgent"

    class DelayReason(models.TextChoices):
        TRAFFIC = "TRAFFIC", "Traffic"
        VEHICLE_ISSUE = "VEHICLE_ISSUE", "Vehicle Issue"
        DRIVER_ISSUE = "DRIVER_ISSUE", "Driver Issue"
        WEATHER = "WEATHER", "Weather"
        CLIENT_DELAY = "CLIENT_DELAY", "Client Delay"
        OTHER = "OTHER", "Other"

    trip_number = models.CharField(max_length=20, unique=True, editable=False)
    trip_type = models.CharField(max_length=15, choices=TripType.choices, default=TripType.DELIVERY)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.NORMAL)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.DRAFT, db_index=True)

    client = models.ForeignKey("clients.Client", on_delete=models.PROTECT, related_name="trips")
    origin_site = models.ForeignKey("locations.Site", on_delete=models.PROTECT, related_name="trips_as_origin")
    destination_site = models.ForeignKey(
        "locations.Site", on_delete=models.PROTECT, related_name="trips_as_destination"
    )
    vehicle = models.ForeignKey(
        "vehicles.Vehicle", null=True, blank=True, on_delete=models.SET_NULL, related_name="trips"
    )
    driver = models.ForeignKey(
        "drivers.Driver", null=True, blank=True, on_delete=models.SET_NULL, related_name="trips"
    )
    route = models.ForeignKey(
        "routes.Route", null=True, blank=True, on_delete=models.SET_NULL, related_name="trips",
        help_text="Optional — a reusable multi-stop itinerary this trip follows. A trip with no route "
                   "still works exactly as before, using origin_site/destination_site alone.",
    )

    scheduled_start = models.DateTimeField()
    scheduled_end = models.DateTimeField()
    actual_start = models.DateTimeField(null=True, blank=True)
    actual_end = models.DateTimeField(null=True, blank=True)

    planned_distance = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True, help_text="km")
    actual_distance = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True, help_text="km")
    fuel_used = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True, help_text="litres")

    instructions = models.TextField(blank=True)
    internal_notes = models.TextField(blank=True)
    driver_remarks = models.TextField(blank=True)
    completion_notes = models.TextField(blank=True)
    delay_reason = models.CharField(max_length=20, choices=DelayReason.choices, blank=True)
    delay_notes = models.TextField(blank=True)
    cancellation_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-scheduled_start"]
        indexes = [
            models.Index(fields=["trip_number"]),
            models.Index(fields=["status"]),
            models.Index(fields=["scheduled_start"]),
        ]

    def __str__(self):
        return self.trip_number

    def clean(self):
        super().clean()
        if self.scheduled_start and self.scheduled_end and self.scheduled_end <= self.scheduled_start:
            raise ValidationError({"scheduled_end": "Scheduled end must be after scheduled start."})
        if self.origin_site_id and self.destination_site_id and self.origin_site_id == self.destination_site_id:
            raise ValidationError({"destination_site": "Origin and destination sites can't be the same."})
        if self.client_id:
            if self.origin_site_id and self.origin_site.client_id != self.client_id:
                raise ValidationError({"origin_site": "Origin site must belong to the selected client."})
            if self.destination_site_id and self.destination_site.client_id != self.client_id:
                raise ValidationError({"destination_site": "Destination site must belong to the selected client."})
            if self.vehicle_id and self.vehicle.client_id and self.vehicle.client_id != self.client_id:
                raise ValidationError({"vehicle": "This vehicle belongs to a different client."})
            if self.driver_id and self.driver.client_id and self.driver.client_id != self.client_id:
                raise ValidationError({"driver": "This driver belongs to a different client."})

    def can_transition_to(self, new_status):
        return new_status in self.VALID_TRANSITIONS.get(self.status, set())

    @property
    def route_label(self):
        return f"{self.origin_site.site_name} → {self.destination_site.site_name}"

    @property
    def estimated_duration(self):
        if not (self.scheduled_start and self.scheduled_end):
            return None
        return self.scheduled_end - self.scheduled_start

    @property
    def is_on_time(self):
        """None when not yet completed or no schedule to compare against —
        callers must treat None as "not applicable", not as False."""
        if self.status != self.Status.COMPLETED or not (self.actual_end and self.scheduled_end):
            return None
        return self.actual_end <= self.scheduled_end
