import datetime

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.core.managers import AllObjectsManager, SoftDeleteManager, SoftDeleteQuerySet
from apps.core.models import BaseFleetModel, TimeStampedModel, UUIDModel


class MaintenanceNumberSequence(models.Model):
    """Singleton row whose value is incremented under a row lock to hand out
    collision-safe maintenance numbers (MNT-000001, MNT-000002, ...) — same
    pattern as apps.trips.models.TripNumberSequence."""

    id = models.SmallIntegerField(primary_key=True, default=1)
    last_value = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"MaintenanceNumberSequence(last_value={self.last_value})"


class MaintenanceQuerySet(SoftDeleteQuerySet):
    """Centralizes the overdue/due-soon definitions as real queryset filters
    so the dashboard, the list filter, and the model's own is_overdue
    property all agree on the same rule instead of three copies of it."""

    def overdue(self):
        return self.filter(status=Maintenance.Status.SCHEDULED, scheduled_date__lt=timezone.now().date())

    def due_today(self):
        return self.filter(status=Maintenance.Status.SCHEDULED, scheduled_date=timezone.now().date())

    def due_soon(self, days=None):
        days = Maintenance.DUE_SOON_WINDOW_DAYS if days is None else days
        today = timezone.now().date()
        return self.filter(
            status=Maintenance.Status.SCHEDULED,
            scheduled_date__gt=today,
            scheduled_date__lte=today + datetime.timedelta(days=days),
        )


class MaintenanceManager(SoftDeleteManager):
    def get_queryset(self):
        return MaintenanceQuerySet(self.model, using=self._db).filter(is_deleted=False)

    def overdue(self):
        return self.get_queryset().overdue()

    def due_today(self):
        return self.get_queryset().due_today()

    def due_soon(self, days=None):
        return self.get_queryset().due_soon(days)


class Maintenance(BaseFleetModel):
    """A single service job on a vehicle — preventive, corrective, or
    inspection work tracked from scheduling through completion.

    "Overdue" is deliberately NOT a stored status (see Status below) —
    it's computed from scheduled_date + status, the same pattern
    apps.drivers.models.Driver.license_expiry_status uses, so nothing needs
    a background job to flip it.
    """

    class MaintenanceType(models.TextChoices):
        PREVENTIVE = "PREVENTIVE", "Preventive"
        CORRECTIVE = "CORRECTIVE", "Corrective"
        INSPECTION = "INSPECTION", "Inspection"
        REPAIR = "REPAIR", "Repair"
        OIL_SERVICE = "OIL_SERVICE", "Oil Service"
        TYRE_SERVICE = "TYRE_SERVICE", "Tyre Service"
        BRAKE_SERVICE = "BRAKE_SERVICE", "Brake Service"
        ELECTRICAL = "ELECTRICAL", "Electrical"
        OTHER = "OTHER", "Other"

    class Status(models.TextChoices):
        SCHEDULED = "SCHEDULED", "Scheduled"
        IN_PROGRESS = "IN_PROGRESS", "In Progress"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    class Priority(models.TextChoices):
        LOW = "LOW", "Low"
        MEDIUM = "MEDIUM", "Medium"
        HIGH = "HIGH", "High"
        CRITICAL = "CRITICAL", "Critical"

    # Single source of truth for the "due soon" window — mirrors
    # Driver.LICENSE_EXPIRY_WARNING_DAYS.
    DUE_SOON_WINDOW_DAYS = 7

    VALID_TRANSITIONS = {
        Status.SCHEDULED: {Status.IN_PROGRESS, Status.CANCELLED},
        Status.IN_PROGRESS: {Status.COMPLETED, Status.CANCELLED},
        Status.COMPLETED: set(),
        Status.CANCELLED: set(),
    }

    maintenance_number = models.CharField(max_length=20, unique=True, editable=False)
    vehicle = models.ForeignKey("vehicles.Vehicle", on_delete=models.PROTECT, related_name="maintenance_records")
    maintenance_type = models.CharField(max_length=20, choices=MaintenanceType.choices, default=MaintenanceType.PREVENTIVE)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.SCHEDULED, db_index=True)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)

    scheduled_date = models.DateField()
    expected_completion_date = models.DateField(null=True, blank=True)
    started_date = models.DateTimeField(null=True, blank=True)
    completed_date = models.DateTimeField(null=True, blank=True)

    odometer_at_service = models.DecimalField(max_digits=10, decimal_places=1)
    next_service_odometer = models.DecimalField(max_digits=10, decimal_places=1, null=True, blank=True)
    next_service_date = models.DateField(null=True, blank=True)

    service_center = models.CharField(max_length=150, blank=True)
    technician = models.CharField(max_length=120, blank=True)
    description = models.TextField(blank=True)
    work_performed = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    completion_notes = models.TextField(blank=True)
    cancellation_reason = models.TextField(blank=True)

    estimated_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    labor_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    actual_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    objects = MaintenanceManager()
    all_objects = AllObjectsManager()

    class Meta:
        ordering = ["-scheduled_date"]
        indexes = [
            models.Index(fields=["maintenance_number"]),
            models.Index(fields=["status"]),
            models.Index(fields=["scheduled_date"]),
        ]
        verbose_name_plural = "Maintenance records"

    def __str__(self):
        return self.maintenance_number

    def clean(self):
        super().clean()
        if self.expected_completion_date and self.scheduled_date and self.expected_completion_date < self.scheduled_date:
            raise ValidationError({"expected_completion_date": "Expected completion can't be before the scheduled date."})

    def can_transition_to(self, new_status):
        return new_status in self.VALID_TRANSITIONS.get(self.status, set())

    @property
    def is_overdue(self):
        return self.status == self.Status.SCHEDULED and self.scheduled_date < timezone.now().date()

    @property
    def overdue_days(self):
        if not self.is_overdue:
            return 0
        return (timezone.now().date() - self.scheduled_date).days

    @property
    def overdue_label(self):
        days = self.overdue_days
        if not days:
            return ""
        return f"{days} day overdue" if days == 1 else f"{days} days overdue"

    @property
    def urgency(self):
        """Centralized semantic state for badges: NORMAL/DUE_SOON/DUE_TODAY/
        OVERDUE. Only meaningful while still Scheduled — once work has
        started or finished, "how urgent was it" no longer applies."""
        if self.status != self.Status.SCHEDULED:
            return None
        today = timezone.now().date()
        if self.scheduled_date < today:
            return "OVERDUE"
        if self.scheduled_date == today:
            return "DUE_TODAY"
        if (self.scheduled_date - today).days <= self.DUE_SOON_WINDOW_DAYS:
            return "DUE_SOON"
        return "NORMAL"

    @property
    def parts_cost(self):
        total = self.parts.aggregate(total=models.Sum(models.F("quantity") * models.F("unit_cost")))["total"]
        return total or 0

    @property
    def cost_variance(self):
        if self.actual_cost is None or self.estimated_cost is None:
            return None
        return self.actual_cost - self.estimated_cost

    @property
    def duration(self):
        if not (self.started_date and self.completed_date):
            return None
        return self.completed_date - self.started_date


class MaintenancePart(UUIDModel, TimeStampedModel):
    """A part/consumable used on a maintenance job. Deliberately not an
    inventory system — just clean line-item cost tracking per job, per the
    "don't build inventory management" scope decision for this pass."""

    maintenance = models.ForeignKey(Maintenance, on_delete=models.CASCADE, related_name="parts")
    part_name = models.CharField(max_length=150)
    part_number = models.CharField(max_length=60, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    class Meta:
        ordering = ["part_name"]

    def __str__(self):
        return f"{self.part_name} x{self.quantity}"

    @property
    def total_cost(self):
        return self.quantity * self.unit_cost
