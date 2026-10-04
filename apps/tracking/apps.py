import os
import sys

from django.apps import AppConfig


class TrackingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tracking"
    label = "tracking"
    verbose_name = "GPS Tracking Devices"

    def ready(self):
        """Start the comms -> FMS feed with the dev server when COMMS_SYNC_AUTOSTART is on.

        Only the process that actually serves requests starts it: under
        ``runserver`` that is the auto-reloader's child (RUN_MAIN=true), or the
        single process with ``--noreload``. Management commands, tests and the
        reloader's watcher process never do. WSGI/ASGI servers start it from
        config/wsgi.py and config/asgi.py."""
        from django.conf import settings

        if not (settings.COMMS_SYNC_AUTOSTART and settings.COMMS_APP_DATABASE_URL):
            return
        argv = sys.argv
        if len(argv) > 1 and argv[1] == "runserver" and (os.environ.get("RUN_MAIN") == "true" or "--noreload" in argv):
            from apps.tracking.comms_runner import start_background_sync

            start_background_sync()
