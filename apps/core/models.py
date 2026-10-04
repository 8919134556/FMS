import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.managers import AllObjectsManager, SoftDeleteManager


class UUIDModel(models.Model):
    """Adds a public, non-sequential identifier separate from the primary key.

    The integer primary key stays internal (fast joins/indexes); ``uuid`` is what
    gets exposed in URLs and the API so record counts/order are never leaked.
    """

    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)

    class Meta:
        abstract = True


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AuditableModel(models.Model):
    """Tracks which user created/last-updated a row."""

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        abstract = True


class SoftDeleteModel(models.Model):
    """Archive instead of delete for critical master data.

    ``objects`` (the default manager) only ever returns live rows, so existing
    queries/list views never need to know soft delete exists. ``all_objects``
    is the escape hatch for admin/restore screens that need deleted rows too.
    """

    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        abstract = True

    def delete(self, using=None, keep_parents=False, deleted_by=None):
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.deleted_by = deleted_by
        self.save(using=using, update_fields=["is_deleted", "deleted_at", "deleted_by"])

    def hard_delete(self, using=None, keep_parents=False):
        return models.Model.delete(self, using=using, keep_parents=keep_parents)

    def restore(self):
        self.is_deleted = False
        self.deleted_at = None
        self.deleted_by = None
        self.save(update_fields=["is_deleted", "deleted_at", "deleted_by"])


class BaseFleetModel(UUIDModel, TimeStampedModel, AuditableModel, SoftDeleteModel):
    """Standard base for master/entity data: UUID + timestamps + audit + soft delete."""

    class Meta:
        abstract = True


class SystemSettings(TimeStampedModel, AuditableModel):
    """App-wide configuration — one row, same row-locked singleton pattern as
    DocumentNumberSequence/TripNumberSequence (``id`` pinned to 1).

    No UUID/soft-delete: this isn't master data with a lifecycle, it's a
    single settings record. ``load()`` is the only way callers should reach
    it — it self-creates on first access so every environment (including a
    freshly migrated one) has a row with sane defaults, matching the current
    hardcoded values Document/Maintenance already advertise, so wiring this
    in changes nothing until an admin edits it.
    """

    class Currency(models.TextChoices):
        INR = "INR", "Indian Rupee (INR)"
        USD = "USD", "US Dollar (USD)"
        EUR = "EUR", "Euro (EUR)"
        GBP = "GBP", "British Pound (GBP)"
        AED = "AED", "UAE Dirham (AED)"

    class DateFormat(models.TextChoices):
        DMY = "DMY", "DD/MM/YYYY"
        MDY = "MDY", "MM/DD/YYYY"
        YMD = "YMD", "YYYY-MM-DD"

    id = models.SmallIntegerField(primary_key=True, default=1)

    company_name = models.CharField(max_length=150, blank=True, help_text="Overrides the default app name shown in the sidebar/browser tab when set.")
    support_email = models.EmailField(blank=True)
    support_phone = models.CharField(max_length=20, blank=True)

    default_timezone = models.CharField(max_length=50, default="Asia/Kolkata")
    default_currency = models.CharField(max_length=3, choices=Currency.choices, default=Currency.INR)
    date_format = models.CharField(max_length=3, choices=DateFormat.choices, default=DateFormat.DMY)

    document_expiry_warning_days = models.PositiveSmallIntegerField(
        default=30, help_text="Documents expiring within this many days are flagged as 'expiring soon'."
    )
    maintenance_due_soon_days = models.PositiveSmallIntegerField(
        default=7, help_text="Scheduled maintenance due within this many days is flagged as 'due soon'."
    )

    class Meta:
        verbose_name = "System Settings"
        verbose_name_plural = "System Settings"

    def __str__(self):
        return "System Settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
