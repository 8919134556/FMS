"""Background thread that keeps the FMS tables fed from the comms App DB.

Turned on with ``COMMS_SYNC_AUTOSTART=True``. It runs the same
``comms_sync.sync_exclusive()`` pass as ``manage.py sync_comms_data`` every
``COMMS_SYNC_INTERVAL_SECONDS`` (default 10), inside the web process, so
"start the site" is enough — there is no second terminal to forget. The
advisory lock inside ``sync_exclusive`` makes it safe with several web
workers or with the management command also running: only one pass runs at a
time. The thread is a daemon and only logs failures (a comms DB restart just
means the next pass retries).
"""

import logging
import threading

from django.conf import settings
from django.db import connections

from apps.tracking import comms_sync

logger = logging.getLogger(__name__)

STARTUP_DELAY_SECONDS = 2  # let the server finish booting before the first pass

_guard = threading.Lock()
_thread = None
_stop = threading.Event()


def is_enabled():
    return bool(settings.COMMS_SYNC_AUTOSTART and settings.COMMS_APP_DATABASE_URL)


def start_background_sync():
    """Start the thread once per process. Returns True if it was started now."""
    global _thread
    if not is_enabled():
        return False
    with _guard:
        if _thread is not None and _thread.is_alive():
            return False
        _stop.clear()
        _thread = threading.Thread(target=_run, name="comms-sync", daemon=True)
        _thread.start()
    return True


def stop_background_sync(timeout=5):
    """Ask the thread to stop (used by tests / graceful shutdown)."""
    _stop.set()
    thread = _thread
    if thread is not None and thread.is_alive():
        thread.join(timeout)


def _run():
    interval = max(1, int(settings.COMMS_SYNC_INTERVAL_SECONDS))
    logger.info("comms sync: background feed started (every %ss)", interval)
    _stop.wait(STARTUP_DELAY_SECONDS)
    while not _stop.is_set():
        try:
            comms_sync.sync_exclusive()
        except Exception as exc:  # never let the feed die; the next pass retries
            logger.warning("comms sync pass failed (will retry): %s", exc)
        finally:
            connections.close_all()  # this thread's own connections; don't hold them between passes
        _stop.wait(interval)
    logger.info("comms sync: background feed stopped")
