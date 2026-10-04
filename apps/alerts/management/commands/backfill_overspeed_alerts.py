from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.tracking import comms_sync


class Command(BaseCommand):
    help = (
        "Flags the comms Raw DB's eventioval-255 (Over Speeding) records on GPS history imported before the bridge "
        "read eventioval, and builds the Over Speeding alerts for that period — without notifications. Idempotent."
    )

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=7, help="How many days back (default 7).")

    def handle(self, *args, **options):
        if not settings.COMMS_RAW_DATABASE_URL:
            raise CommandError("COMMS_RAW_DATABASE_URL is not set.")
        if options["days"] < 1:
            raise CommandError("--days must be at least 1.")
        result = comms_sync.backfill_overspeed_flags(days=options["days"])
        self.stdout.write(self.style.SUCCESS(
            f"Flagged {result['updated']} history record(s); created {result['alerts_created']} Over Speeding alert(s)."
        ))
