import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_wsgi_application()

from apps.tracking.comms_runner import start_background_sync  # noqa: E402  (needs settings/apps loaded)

start_background_sync()  # no-op unless COMMS_SYNC_AUTOSTART is on
