from django.db import models
from django.utils import timezone


class SoftDeleteQuerySet(models.QuerySet):
    """QuerySet with soft-delete-aware bulk operations."""

    def delete(self):
        """Bulk soft delete: mark rows as deleted instead of removing them."""
        return super().update(is_deleted=True, deleted_at=timezone.now())

    def hard_delete(self):
        """Permanently remove rows from the database. Use with extreme care."""
        return super().delete()

    def alive(self):
        return self.filter(is_deleted=False)

    def deleted(self):
        return self.filter(is_deleted=True)


class SoftDeleteManager(models.Manager):
    """Default manager: only returns rows that have not been soft-deleted."""

    def get_queryset(self):
        return SoftDeleteQuerySet(self.model, using=self._db).filter(is_deleted=False)

    def deleted(self):
        return SoftDeleteQuerySet(self.model, using=self._db).filter(is_deleted=True)


class AllObjectsManager(models.Manager):
    """Unfiltered manager: returns every row, including soft-deleted ones."""

    def get_queryset(self):
        return SoftDeleteQuerySet(self.model, using=self._db)
