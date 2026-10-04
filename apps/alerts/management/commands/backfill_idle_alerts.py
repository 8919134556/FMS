from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.alerts import idle
from apps.vehicles.models import Vehicle


class Command(BaseCommand):
    help = (
        "Builds Idle alert events from the GPS history already stored (last --days days), without sending "
        "notifications. Idempotent; new data needs no backfill (alerts are raised as data is ingested)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=7, help="How many days back to scan (default 7).")
        parser.add_argument("--vehicle", default="", help="Registration number of one vehicle (default: all tracked).")

    def handle(self, *args, **options):
        if options["days"] < 1:
            raise CommandError("--days must be at least 1.")
        vehicles = Vehicle.objects.filter(tracking_device__isnull=False)
        if options["vehicle"]:
            vehicles = vehicles.filter(registration_number=options["vehicle"])
            if not vehicles.exists():
                raise CommandError(f"No tracked vehicle {options['vehicle']!r}.")
        since = timezone.now() - timezone.timedelta(days=options["days"])
        created = idle.backfill(vehicles=list(vehicles), since=since)
        self.stdout.write(self.style.SUCCESS(f"Created {created} idle alert event(s)."))
