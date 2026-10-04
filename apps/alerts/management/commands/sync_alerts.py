from django.core.management.base import BaseCommand

from apps.alerts import services


class Command(BaseCommand):
    help = "Reconciles Alert rows against live trip/maintenance/document conditions. Safe to run repeatedly (idempotent); intended for cron/Task Scheduler, not required since the Alerts page syncs on view."

    def handle(self, *args, **options):
        result = services.sync_alerts()
        self.stdout.write(
            self.style.SUCCESS(
                f"Synced {result['categories_synced']} categories, {result['seen']} active conditions, "
                f"auto-resolved {result['auto_resolved']} alert(s)."
            )
        )
