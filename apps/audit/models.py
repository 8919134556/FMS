import uuid

from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    """Immutable record of a security- or business-relevant action.

    Deliberately does NOT use ``SoftDeleteModel``/``TimeStampedModel`` — an
    audit entry is never updated or archived, only ever created and read.
    ``save()``/``delete()`` are overridden below to enforce that at the ORM
    level, not just by convention.
    """

    class Action(models.TextChoices):
        CREATE = "CREATE", "Create"
        UPDATE = "UPDATE", "Update"
        ARCHIVE = "ARCHIVE", "Archive"
        RESTORE = "RESTORE", "Restore"
        LOGIN = "LOGIN", "Login"
        LOGOUT = "LOGOUT", "Logout"
        ASSIGN = "ASSIGN", "Assign"
        UNASSIGN = "UNASSIGN", "Unassign"
        APPROVE = "APPROVE", "Approve"
        REJECT = "REJECT", "Reject"
        EXPORT = "EXPORT", "Export"
        DISPATCH = "DISPATCH", "Dispatch"
        START = "START", "Start"
        DELAY = "DELAY", "Delay"
        RESUME = "RESUME", "Resume"
        COMPLETE = "COMPLETE", "Complete"
        CANCEL = "CANCEL", "Cancel"
        DELETE = "DELETE", "Delete"
        DOWNLOAD = "DOWNLOAD", "Download"

    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
        help_text="Null for system-initiated actions (e.g. scheduled jobs).",
    )
    action = models.CharField(max_length=20, choices=Action.choices, db_index=True)
    module = models.CharField(max_length=50, db_index=True)
    entity = models.CharField(max_length=100, help_text="Model name the action applied to, e.g. 'User'.")
    entity_id = models.CharField(max_length=64, blank=True, db_index=True)
    old_value = models.JSONField(null=True, blank=True)
    new_value = models.JSONField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["module", "entity", "entity_id"]),
            models.Index(fields=["-timestamp"]),
        ]
        verbose_name = "Audit Log"
        verbose_name_plural = "Audit Logs"

    def __str__(self):
        return f"{self.get_action_display()} {self.module}/{self.entity}#{self.entity_id} @ {self.timestamp:%Y-%m-%d %H:%M}"

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("Audit log entries are immutable and cannot be modified once created.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Audit log entries are read-only and cannot be deleted.")
