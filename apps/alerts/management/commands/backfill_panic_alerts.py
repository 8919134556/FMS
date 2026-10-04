from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.tracking import comms_sync


class Command(BaseCommand):
    help = (
        "Copies the comms panic flag (and panic voltage) onto GPS history imported before the bridge carried it, "
        "and rebuilds the Panic alert events for that period — without sending notifications. Idempotent; "
        "new data needs no backfill (the comms bridge raises alerts as it imports)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=7, help="How many days back to backfill (default 7).")

    def handle(self, *args, **options):
        if not settings.COMMS_APP_DATABASE_URL:
            raise CommandError("COMMS_APP_DATABASE_URL is not set.")
        if options["days"] < 1:
            raise CommandError("--days must be at least 1.")
        result = comms_sync.backfill_panic_flags(days=options["days"])
        self.stdout.write(self.style.SUCCESS(
            f"Updated {result['updated']} history record(s); created {result['alerts_created']} panic alert event(s)."
        ))
