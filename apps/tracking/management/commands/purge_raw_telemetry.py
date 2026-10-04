from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.tracking.models import RawTelemetryEvent


class Command(BaseCommand):
    help = (
        "Deletes RawTelemetryEvent rows older than TELEMATICS_RAW_EVENT_RETENTION_DAYS (the raw table is a "
        "debugging buffer; normalized history in TelemetryEvent is never touched). Deletes in batches so it "
        "is safe to run on a large table from cron/Task Scheduler."
    )

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=None, help="Override the retention window.")
        parser.add_argument("--batch-size", type=int, default=5000)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        days = options["days"] if options["days"] is not None else settings.TELEMATICS_RAW_EVENT_RETENTION_DAYS
        cutoff = timezone.now() - timezone.timedelta(days=days)
        stale = RawTelemetryEvent.objects.filter(received_at__lt=cutoff)
        if options["dry_run"]:
            self.stdout.write(f"{stale.count()} raw event(s) older than {days} days would be deleted.")
            return
        total = 0
        while True:
            ids = list(stale.values_list("pk", flat=True)[: options["batch_size"]])
            if not ids:
                break
            deleted, _ = RawTelemetryEvent.objects.filter(pk__in=ids).delete()
            total += deleted
        self.stdout.write(self.style.SUCCESS(f"Deleted {total} raw event(s) older than {days} days."))
