"""Entrypoint for the standalone telematics TCP service — a separate OS
process from the web app (WSGI/gunicorn). Run it with:

    python manage.py run_telematics_server

No CLI options: every knob (host/port/timeouts/poll interval) is a Django
setting (see the "Telematics Communication Service" block in
config/settings.py) so behavior is identical whether started by hand,
systemd, or Docker (see docker-compose.yml's telematics-server service).
"""

import asyncio
import logging
import signal

from django.core.management.base import BaseCommand

from apps.tracking.telematics_service import config
from apps.tracking.telematics_service.server import run_server

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Run the standalone asyncio TCP server that accepts GPS/telematics device "
        "connections and delivers queued DeviceCommands. Runs as a separate process "
        "from the web app — safe to Ctrl+C or send SIGTERM for a graceful shutdown."
    )

    def handle(self, *args, **options):
        self.stdout.write(f"Starting telematics TCP server on {config.TCP_HOST}:{config.TCP_PORT} ...")
        try:
            asyncio.run(self._main())
        except KeyboardInterrupt:
            pass
        self.stdout.write(self.style.SUCCESS("Telematics server stopped."))

    async def _main(self):
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                # Not available on Windows dev environments — Ctrl+C raises
                # KeyboardInterrupt instead, handled in handle() above.
                pass
        await run_server(stop_event)
