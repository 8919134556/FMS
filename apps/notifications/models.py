from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel, UUIDModel


class Notification(UUIDModel, TimeStampedModel):
    """A single in-app inbox entry for one user.

    Personal, not master data — no soft-delete/audit-actor fields (a user's
    own inbox is not something anyone but them archives), just a UUID (so
    action URLs never leak sequential inbox size) and a timestamp.

    RBAC's ``notification`` module (see apps.accounts.models.Permission)
    gates a different concern: who may *compose/broadcast* a notification to
    other users (apps.notifications.views.BroadcastNotificationView). Reading,
    marking read, and archiving your own inbox needs no module permission at
    all — every authenticated user manages their own notifications, the same
    way anyone can see their own profile without a "user.view" grant.
    """

    class Level(models.TextChoices):
        INFO = "INFO", "Info"
        SUCCESS = "SUCCESS", "Success"
        WARNING = "WARNING", "Warning"
        CRITICAL = "CRITICAL", "Critical"

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    title = models.CharField(max_length=200)
    body = models.CharField(max_length=500, blank=True)
    level = models.CharField(max_length=10, choices=Level.choices, default=Level.INFO)
    link_url = models.CharField(max_length=300, blank=True, help_text="Where 'View' should take the recipient.")

    is_read = models.BooleanField(default=False, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)
    is_archived = models.BooleanField(default=False, db_index=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        help_text="Who triggered this notification — null for system-generated ones.",
    )
    alert = models.ForeignKey(
        "alerts.Alert", null=True, blank=True, on_delete=models.SET_NULL, related_name="notifications",
        help_text="The alert event this notification announces (panic popup + sound in the bell).",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["recipient", "is_read", "is_archived"])]

    def __str__(self):
        return f"{self.title} -> {self.recipient}"

    def mark_read(self):
        if not self.is_read:
            from django.utils import timezone

            self.is_read = True
            self.read_at = timezone.now()
            self.save(update_fields=["is_read", "read_at"])
