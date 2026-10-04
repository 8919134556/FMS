import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.tracking import comms_sync


class Command(BaseCommand):
    help = (
        "Copies live positions and history from the Zentora comms App DB (current_table/history_table) into the "
        "FMS tracking tables so they show on the Live Map, dashboards and client views. Read-only on the comms "
        "side, incremental, and safe to run repeatedly. Use --loop to keep it running like the comms workers."
    )

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Keep running, syncing every --interval seconds.")
        parser.add_argument("--interval", type=int, default=10, help="Seconds between passes with --loop (default 10).")
        parser.add_argument("--history-days", type=int, default=comms_sync.DEFAULT_HISTORY_DAYS,
                            help="First run only: how many days of history to import (default 7).")
        parser.add_argument("--batch-size", type=int, default=comms_sync.DEFAULT_BATCH_SIZE)
        parser.add_argument("--max-rows", type=int, default=comms_sync.DEFAULT_MAX_ROWS,
                            help="Cap on history rows per pass, so a big backlog is worked off in slices.")

    def handle(self, *args, **options):
        if not settings.COMMS_APP_DATABASE_URL:
            raise CommandError(
                "COMMS_APP_DATABASE_URL is not set. Add it to .env, e.g. "
                "COMMS_APP_DATABASE_URL=postgres://user:password@localhost:5432/APPDB"
            )
        while True:
            try:
                report = comms_sync.sync_exclusive(
                    history_days=options["history_days"], batch_size=options["batch_size"], max_rows=options["max_rows"],
                )
                if report is None:
                    self.stdout.write("Another sync (e.g. the web server's background feed) is running; skipped this pass.")
                    if not options["loop"]:
                        return
                    time.sleep(max(1, options["interval"]))
                    continue
                self.stdout.write(
                    f"current={report.current_rows} history={report.history_rows} "
                    f"imported={report.accepted} rejected={report.rejected} cursor={report.last_history_id}"
                )
                for unit, n in report.unknown_units.items():
                    self.stdout.write(self.style.WARNING(
                        f"  skipped {n} row(s) from IMEI {unit}: no FMS device with that IMEI (Tracking > GPS Devices)."
                    ))
                for unit, n in report.inactive_units.items():
                    self.stdout.write(self.style.WARNING(f"  skipped {n} row(s) from IMEI {unit}: device is not ACTIVE."))
            except Exception as exc:  # the loop must survive a comms DB restart
                if not options["loop"]:
                    raise CommandError(f"Sync failed: {exc}")
                self.stderr.write(self.style.ERROR(f"Sync failed (will retry): {exc}"))
            if not options["loop"]:
                return
            time.sleep(max(1, options["interval"]))
