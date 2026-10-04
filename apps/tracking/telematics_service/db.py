"""The ONE place any ORM call crosses from the asyncio event loop into
Django's synchronous ORM.

Django's DB connections are thread-local and (per config/settings.py's
DATABASES block) default to close-after-use — no CONN_MAX_AGE/pooling is
configured. asgiref.sync.sync_to_async(thread_sensitive=False) runs each
call on a thread from its own executor pool, so every call here explicitly
closes any connection left open on that thread afterward
(django.db.close_old_connections) to avoid leaking/stale per-thread
connections across the pool.
"""

from asgiref.sync import sync_to_async
from django.db import close_old_connections


def _call_and_close(fn, args, kwargs):
    try:
        return fn(*args, **kwargs)
    finally:
        close_old_connections()


async def run_sync(fn, *args, **kwargs):
    """Runs a synchronous function (typically an ORM operation) off the
    event loop thread, closing any DB connection it leaves open afterward."""
    return await sync_to_async(_call_and_close, thread_sensitive=False)(fn, args, kwargs)
