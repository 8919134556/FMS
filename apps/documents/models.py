import datetime
import uuid as uuid_lib

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone

from apps.core.managers import AllObjectsManager, SoftDeleteManager, SoftDeleteQuerySet
from apps.core.models import BaseFleetModel
from apps.core.validators import validate_document_content, validate_document_extension, validate_file_size


def document_upload_path(instance, filename):
    """Storage name is a random UUID, never the user-supplied filename —
    the original is preserved separately in ``original_filename`` for
    display/download. Prevents path traversal, collisions, and leaking
    other users' filenames through guessable URLs."""
    import os

    ext = os.path.splitext(filename)[1].lower()
    today = timezone.now()
    return f"documents/{today.year}/{today.month:02d}/{uuid_lib.uuid4().hex}{ext}"


class DocumentNumberSequence(models.Model):
    """Same row-locked singleton-counter pattern as TripNumberSequence /
    MaintenanceNumberSequence — collision-safe DOC-000001 numbering."""

    id = models.SmallIntegerField(primary_key=True, default=1)
    last_value = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"DocumentNumberSequence(last_value={self.last_value})"


class DocumentQuerySet(SoftDeleteQuerySet):
    """Centralizes the expiry-window definitions as real queryset filters,
    the same pattern apps.maintenance.models.MaintenanceQuerySet uses for
    overdue/due-soon, so the model property, the list filter, and the
    dashboard KPI all agree on one definition."""

    def active(self):
        return self.filter(status=Document.Status.ACTIVE)

    def expired(self):
        return self.filter(expiry_date__isnull=False, expiry_date__lt=timezone.now().date())

    def expiring_soon(self, days=None):
        days = Document.EXPIRING_SOON_WINDOW_DAYS if days is None else days
        today = timezone.now().date()
        return self.filter(expiry_date__isnull=False, expiry_date__gte=today, expiry_date__lte=today + datetime.timedelta(days=days))

    def valid(self, days=None):
        days = Document.EXPIRING_SOON_WINDOW_DAYS if days is None else days
        today = timezone.now().date()
        return self.filter(expiry_date__isnull=False, expiry_date__gt=today + datetime.timedelta(days=days))

    def no_expiry(self):
        return self.filter(expiry_date__isnull=True)

    def current_versions(self):
        """Excludes documents that have since been replaced by a newer
        version — what entity-scoped Documents tabs should show by default."""
        return self.filter(superseded_by__isnull=True)


class DocumentManager(SoftDeleteManager):
    def get_queryset(self):
        return DocumentQuerySet(self.model, using=self._db).filter(is_deleted=False)

    def active(self):
        return self.get_queryset().active()

    def expired(self):
        return self.get_queryset().expired()

    def expiring_soon(self, days=None):
        return self.get_queryset().expiring_soon(days)

    def valid(self, days=None):
        return self.get_queryset().valid(days)

    def no_expiry(self):
        return self.get_queryset().no_expiry()

    def current_versions(self):
        return self.get_queryset().current_versions()


class Document(BaseFleetModel):
    """A file attached to any FMS entity (Client/Site/Vehicle/Driver/Trip/
    Maintenance) via a generic relation, instead of one duplicate
    ClientDocument/VehicleDocument/... model per entity.

    "Expired" is deliberately NOT a stored status — see DocumentQuerySet —
    the same computed-not-stored pattern as Driver.license_expiry_status
    and Maintenance.is_overdue, so nothing needs a background job to flip
    a document to "expired" the moment its date passes.
    """

    class DocumentType(models.TextChoices):
        # Vehicle
        RC = "RC", "Registration Certificate"
        INSURANCE = "INSURANCE", "Insurance"
        PUC = "PUC", "Pollution Certificate"
        FITNESS = "FITNESS", "Fitness Certificate"
        PERMIT = "PERMIT", "Permit"
        SERVICE_RECORD = "SERVICE_RECORD", "Service Record"
        # Driver
        DRIVING_LICENSE = "DRIVING_LICENSE", "Driving License"
        ID_PROOF = "ID_PROOF", "ID Proof"
        MEDICAL_CERTIFICATE = "MEDICAL_CERTIFICATE", "Medical Certificate"
        TRAINING_CERTIFICATE = "TRAINING_CERTIFICATE", "Training Certificate"
        # Client / Site
        CONTRACT = "CONTRACT", "Contract"
        AGREEMENT = "AGREEMENT", "Agreement"
        TAX_CERTIFICATE = "TAX_CERTIFICATE", "Tax Certificate"
        REGISTRATION = "REGISTRATION", "Registration"
        ADDRESS_PROOF = "ADDRESS_PROOF", "Address Proof"
        # Trip
        TRIP_SHEET = "TRIP_SHEET", "Trip Sheet"
        DELIVERY_PROOF = "DELIVERY_PROOF", "Delivery Proof"
        INVOICE = "INVOICE", "Invoice"
        # Maintenance
        SERVICE_INVOICE = "SERVICE_INVOICE", "Service Invoice"
        REPAIR_ESTIMATE = "REPAIR_ESTIMATE", "Repair Estimate"
        WARRANTY = "WARRANTY", "Warranty"
        # Universal
        OTHER = "OTHER", "Other"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        ARCHIVED = "ARCHIVED", "Archived"

    # Single source of truth for the "expiring soon" window — mirrors
    # Driver.LICENSE_EXPIRY_WARNING_DAYS / Maintenance.DUE_SOON_WINDOW_DAYS.
    EXPIRING_SOON_WINDOW_DAYS = 30

    document_number = models.CharField(max_length=20, unique=True, editable=False)
    title = models.CharField(max_length=200)
    document_type = models.CharField(max_length=30, choices=DocumentType.choices, default=DocumentType.OTHER)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.ACTIVE, db_index=True)

    file = models.FileField(
        upload_to=document_upload_path,
        validators=[validate_file_size, validate_document_extension, validate_document_content],
    )
    original_filename = models.CharField(max_length=255, editable=False)
    file_size = models.PositiveIntegerField(editable=False, help_text="bytes")
    mime_type = models.CharField(max_length=100, blank=True, editable=False)

    issue_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)

    version = models.PositiveIntegerField(default=1)
    replaces = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="superseded_by"
    )

    notes = models.TextField(blank=True)

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()
    content_object = GenericForeignKey("content_type", "object_id")

    objects = DocumentManager()
    all_objects = AllObjectsManager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["document_number"]),
            models.Index(fields=["content_type", "object_id"]),
            models.Index(fields=["status"]),
            models.Index(fields=["expiry_date"]),
        ]

    def __str__(self):
        return f"{self.document_number} — {self.title}"

    @property
    def is_expired(self):
        return bool(self.expiry_date and self.expiry_date < timezone.now().date())

    @property
    def is_expiring_soon(self):
        if not self.expiry_date or self.is_expired:
            return False
        return (self.expiry_date - timezone.now().date()).days <= self.EXPIRING_SOON_WINDOW_DAYS

    @property
    def days_until_expiry(self):
        if not self.expiry_date:
            return None
        return (self.expiry_date - timezone.now().date()).days

    @property
    def expiry_status(self):
        """Centralized semantic state for badges: EXPIRED/EXPIRING_SOON/
        VALID/NO_EXPIRY — the single definition every view, template, and
        the dashboard reads instead of re-deriving the same date math."""
        if not self.expiry_date:
            return "NO_EXPIRY"
        if self.is_expired:
            return "EXPIRED"
        if self.is_expiring_soon:
            return "EXPIRING_SOON"
        return "VALID"

    @property
    def expiry_label(self):
        """Human-readable expiry line for badges/tables — centralizes the
        "12 days left" / "Expired 23 days ago" phrasing in one place."""
        if not self.expiry_date:
            return "Never"
        days = self.days_until_expiry
        if days < 0:
            days_ago = abs(days)
            return f"Expired {days_ago} day{'s' if days_ago != 1 else ''} ago"
        if days == 0:
            return "Expires today"
        return f"{days} day{'s' if days != 1 else ''} left"

    @property
    def is_current_version(self):
        try:
            return self.superseded_by is None
        except Document.DoesNotExist:
            return True

    @property
    def is_previewable(self):
        import os

        ext = os.path.splitext(self.original_filename)[1].lower()
        return ext in {".pdf", ".jpg", ".jpeg", ".png"}

    @property
    def file_size_display(self):
        size = self.file_size or 0
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"
