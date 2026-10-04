import datetime

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.core.models import BaseFleetModel, TimeStampedModel, UUIDModel
from apps.core.validators import validate_file_size, validate_image_extension


# ---------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------
class VehicleCategory(UUIDModel, TimeStampedModel):
    """Broad grouping, e.g. Light Commercial / Heavy Commercial / Passenger."""

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=80)
    description = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "Vehicle categories"

    def __str__(self):
        return self.name


class VehicleType(UUIDModel, TimeStampedModel):
    """Body/type master, e.g. Sedan, SUV, Truck, Bus, Van, Tempo Traveller."""

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=80)
    category = models.ForeignKey(
        VehicleCategory, null=True, blank=True, on_delete=models.PROTECT, related_name="vehicle_types"
    )
    description = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class FuelType(UUIDModel, TimeStampedModel):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=60)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


def vehicle_image_upload_path(instance, filename):
    return f"vehicle_photos/{instance.uuid}/{filename}"


CURRENT_YEAR = datetime.date.today().year

# Trip closure time (see Vehicle.trip_closure_minutes and apps.tracking.trip_report):
# how long ignition must stay OFF before the vehicle's current trip is closed.
# 5 is the value every vehicle used before it became configurable, so existing
# vehicles (migrated to it) and new ones keep the same trip behaviour until changed.
DEFAULT_TRIP_CLOSURE_MINUTES = 5
# One day. Trip detection looks back 2 days of history (trip_report.LOOKBACK),
# so a closure time longer than that could never be honoured.
MAX_TRIP_CLOSURE_MINUTES = 1440

# Trip START validation (see Vehicle.trip_validation_* and
# apps.tracking.trip_report): ignition ON only opens a CANDIDATE trip; it is
# confirmed once the history shows genuine movement. Defaults, applied to every
# vehicle until changed on the Vehicle screen.
DEFAULT_TRIP_VALIDATION_RECORDS = 5  # sliding window of readings examined
DEFAULT_TRIP_MIN_MOVING_RECORDS = 3  # readings in that window at/above the speed
DEFAULT_TRIP_MIN_SPEED_KMH = 5  # = TELEMATICS_MOVEMENT_SPEED_THRESHOLD_KMH default
DEFAULT_TRIP_MIN_DISTANCE_M = 50  # displacement that proves the vehicle left its spot

# Idle alert (apps.alerts.idle): ignition ON and stationary — no movement by the
# two settings above — continuously for this long raises one MEDIUM Idle alert.
DEFAULT_IDLE_ALERT_MINUTES = 5
MAX_IDLE_ALERT_MINUTES = 240


# ---------------------------------------------------------------------------
# Vehicle
# ---------------------------------------------------------------------------
class Vehicle(BaseFleetModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"
        UNDER_MAINTENANCE = "UNDER_MAINTENANCE", "Under Maintenance"
        ACCIDENT = "ACCIDENT", "Accident"
        SOLD = "SOLD", "Sold"
        RETIRED = "RETIRED", "Retired"
        SCRAPPED = "SCRAPPED", "Scrapped"

    class AvailabilityStatus(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Available"
        ASSIGNED = "ASSIGNED", "Assigned"
        ON_TRIP = "ON_TRIP", "On Trip"
        MAINTENANCE = "MAINTENANCE", "Maintenance"
        UNAVAILABLE = "UNAVAILABLE", "Unavailable"

    class TransmissionType(models.TextChoices):
        MANUAL = "MANUAL", "Manual"
        AUTOMATIC = "AUTOMATIC", "Automatic"
        AMT = "AMT", "AMT"
        CVT = "CVT", "CVT"

    class OwnershipType(models.TextChoices):
        OWNED = "OWNED", "Owned"
        LEASED = "LEASED", "Leased"
        RENTED = "RENTED", "Rented"
        VENDOR_OWNED = "VENDOR_OWNED", "Vendor-Owned"

    class OdometerUnit(models.TextChoices):
        KM = "KM", "Kilometers"
        MILES = "MILES", "Miles"

    # 1. Identification
    registration_number = models.CharField(max_length=20, unique=True)
    vehicle_code = models.CharField(max_length=20, unique=True)
    vin = models.CharField("VIN", max_length=32, unique=True, null=True, blank=True)
    chassis_number = models.CharField(max_length=40, unique=True, null=True, blank=True)
    engine_number = models.CharField(max_length=40, unique=True, null=True, blank=True)

    # 2. Vehicle information
    vehicle_type = models.ForeignKey(VehicleType, on_delete=models.PROTECT, related_name="vehicles")
    category = models.ForeignKey(
        VehicleCategory, null=True, blank=True, on_delete=models.PROTECT, related_name="vehicles"
    )
    manufacturer = models.CharField(max_length=80, blank=True)
    make = models.CharField(max_length=80)
    model = models.CharField(max_length=80)
    variant = models.CharField(max_length=80, blank=True)
    model_year = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1980), MaxValueValidator(CURRENT_YEAR + 1)]
    )
    manufacturing_date = models.DateField(null=True, blank=True)
    color = models.CharField(max_length=40, blank=True)

    # 3. Technical
    fuel_type = models.ForeignKey(FuelType, on_delete=models.PROTECT, related_name="vehicles")
    transmission_type = models.CharField(max_length=15, choices=TransmissionType.choices, blank=True)
    engine_capacity = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True, help_text="cc")
    seating_capacity = models.PositiveSmallIntegerField(null=True, blank=True)
    load_capacity = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="kg")
    mileage = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True, help_text="km/l")

    # 4. Ownership
    ownership_type = models.CharField(max_length=15, choices=OwnershipType.choices, default=OwnershipType.OWNED)
    owner_name = models.CharField(max_length=150, blank=True)
    vendor = models.ForeignKey(
        "vendors.Vendor", null=True, blank=True, on_delete=models.SET_NULL, related_name="vehicles"
    )
    # The tenant a vehicle belongs to (NULL = company-owned/unassigned).
    # PROTECT: hard-deleting a client must not silently orphan its fleet into
    # the unassigned pool; clients are archived (soft-deleted), not removed.
    client = models.ForeignKey(
        "clients.Client", null=True, blank=True, on_delete=models.PROTECT, related_name="vehicles"
    )
    contract = models.ForeignKey(
        "contracts.Contract", null=True, blank=True, on_delete=models.SET_NULL, related_name="vehicles"
    )
    branch = models.ForeignKey(
        "locations.Branch", null=True, blank=True, on_delete=models.SET_NULL, related_name="vehicles"
    )

    # 5. Operational
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    availability_status = models.CharField(
        max_length=15, choices=AvailabilityStatus.choices, default=AvailabilityStatus.AVAILABLE, db_index=True
    )
    current_driver = models.ForeignKey(
        "drivers.Driver", null=True, blank=True, on_delete=models.SET_NULL, related_name="current_vehicles"
    )
    odometer_reading = models.DecimalField(max_digits=10, decimal_places=1, default=0)
    odometer_unit = models.CharField(max_length=10, choices=OdometerUnit.choices, default=OdometerUnit.KM)
    # Per-vehicle trip rule, read by apps.tracking.trip_report for THIS vehicle
    # only: ignition OFF for less than this keeps the current trip open (a
    # red light, a delivery stop); OFF for this long or longer closes it, and
    # the next ignition ON starts a new trip.
    trip_closure_minutes = models.PositiveSmallIntegerField(
        "Trip closure time (minutes)",
        default=DEFAULT_TRIP_CLOSURE_MINUTES,
        validators=[MinValueValidator(1), MaxValueValidator(MAX_TRIP_CLOSURE_MINUTES)],
        help_text="Ignition OFF for this many minutes or more closes the current trip; "
        "switching back ON sooner continues the same trip.",
    )
    # Trip start validation — per vehicle, beside the closure time above.
    trip_validation_records = models.PositiveSmallIntegerField(
        "Validation records",
        default=DEFAULT_TRIP_VALIDATION_RECORDS,
        validators=[MinValueValidator(2), MaxValueValidator(20)],
        help_text="How many consecutive readings are examined when deciding whether the vehicle is moving.",
    )
    trip_min_moving_records = models.PositiveSmallIntegerField(
        "Required moving records",
        default=DEFAULT_TRIP_MIN_MOVING_RECORDS,
        validators=[MinValueValidator(1), MaxValueValidator(20)],
        help_text="How many of those readings must be at or above the minimum speed.",
    )
    trip_min_speed_kmh = models.PositiveSmallIntegerField(
        "Minimum movement speed (km/h)",
        default=DEFAULT_TRIP_MIN_SPEED_KMH,
        validators=[MinValueValidator(1), MaxValueValidator(60)],
        help_text="A reading at or above this speed counts as moving.",
    )
    trip_min_distance_m = models.PositiveSmallIntegerField(
        "Minimum movement distance (m)",
        default=DEFAULT_TRIP_MIN_DISTANCE_M,
        validators=[MinValueValidator(10), MaxValueValidator(2000)],
        help_text="How far the vehicle must get from where the ignition came on before a trip is confirmed.",
    )
    # Idle alert threshold. "Stationary" uses the two movement settings above
    # (speed / distance), so trips and idle share one definition of movement.
    idle_alert_minutes = models.PositiveSmallIntegerField(
        "Idle alert after (minutes)",
        default=DEFAULT_IDLE_ALERT_MINUTES,
        validators=[MinValueValidator(1), MaxValueValidator(MAX_IDLE_ALERT_MINUTES)],
        help_text="Ignition ON and stationary continuously for this long raises an Idle alert.",
    )

    # Service tracking — written by apps.maintenance.services.complete_maintenance
    # when a maintenance job completes; read by the dashboard/vehicle detail
    # "maintenance due" indicators. Additive fields, same pattern as the rest
    # of this model gaining fields as the module that needs them ships.
    last_service_date = models.DateField(null=True, blank=True)
    last_service_odometer = models.DecimalField(max_digits=10, decimal_places=1, null=True, blank=True)
    next_service_date = models.DateField(null=True, blank=True)
    next_service_odometer = models.DecimalField(max_digits=10, decimal_places=1, null=True, blank=True)

    # 6. Financial
    purchase_date = models.DateField(null=True, blank=True)
    purchase_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    current_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    # Other
    image = models.ImageField(
        upload_to=vehicle_image_upload_path, null=True, blank=True,
        validators=[validate_file_size, validate_image_extension],
    )
    remarks = models.TextField(blank=True)

    class Meta:
        ordering = ["registration_number"]
        indexes = [
            models.Index(fields=["registration_number"]),
            models.Index(fields=["vin"]),
            models.Index(fields=["status"]),
            models.Index(fields=["availability_status"]),
        ]

    def __str__(self):
        return f"{self.registration_number} ({self.make} {self.model})"

    def clean(self):
        super().clean()
        if self.manufacturing_date and self.purchase_date and self.purchase_date < self.manufacturing_date:
            raise ValidationError({"purchase_date": "Purchase date can't be before the manufacturing date."})
        if (
            self.trip_min_moving_records is not None
            and self.trip_validation_records is not None
            and self.trip_min_moving_records > self.trip_validation_records
        ):
            raise ValidationError({
                "trip_min_moving_records": "Can't require more moving records than the validation records examined."
            })


class VehicleDriverAssignment(BaseFleetModel):
    class AssignmentType(models.TextChoices):
        PRIMARY = "PRIMARY", "Primary"
        SECONDARY = "SECONDARY", "Secondary"
        TEMPORARY = "TEMPORARY", "Temporary"
        RELIEF = "RELIEF", "Relief"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        ENDED = "ENDED", "Ended"
        CANCELLED = "CANCELLED", "Cancelled"

    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE, related_name="driver_assignments")
    driver = models.ForeignKey("drivers.Driver", on_delete=models.CASCADE, related_name="vehicle_assignments")
    assignment_type = models.CharField(max_length=15, choices=AssignmentType.choices, default=AssignmentType.PRIMARY)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    primary_driver = models.BooleanField(default=True)
    assigned_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    remarks = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.ACTIVE, db_index=True)

    class Meta:
        ordering = ["-start_date"]
        indexes = [models.Index(fields=["vehicle", "status"]), models.Index(fields=["driver", "status"])]
        constraints = [
            # A vehicle can only have one *current* primary driver at a time.
            # Reassigning ends the previous assignment first (see
            # apps.vehicles.services.assign_driver) rather than violating this.
            models.UniqueConstraint(
                fields=["vehicle"],
                condition=models.Q(status="ACTIVE", primary_driver=True),
                name="uniq_active_primary_assignment_per_vehicle",
            )
        ]

    def __str__(self):
        return f"{self.driver} -> {self.vehicle} ({self.status})"

    def clean(self):
        super().clean()
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "End date can't be before the start date."})
