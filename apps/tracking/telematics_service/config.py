"""Module-level constants read once from Django settings at import time — a
long-running server process doesn't need live settings reload. See the
"Telematics Communication Service" block in config/settings.py for defaults
and the environment variables that override them.
"""

from django.conf import settings

TCP_HOST = settings.TELEMATICS_TCP_HOST
TCP_PORT = settings.TELEMATICS_TCP_PORT
MAX_FRAME_BYTES = settings.TELEMATICS_MAX_FRAME_BYTES
IDENTIFY_TIMEOUT_SECONDS = settings.TELEMATICS_IDENTIFY_TIMEOUT_SECONDS
CONNECTION_IDLE_TIMEOUT_SECONDS = settings.TELEMATICS_CONNECTION_IDLE_TIMEOUT_SECONDS
COMMAND_POLL_INTERVAL_SECONDS = settings.TELEMATICS_COMMAND_POLL_INTERVAL_SECONDS
COMMAND_ACK_TIMEOUT_SECONDS = settings.TELEMATICS_COMMAND_ACK_TIMEOUT_SECONDS

# Teltonika Codec 8 adapter (Phase 3.6) — separate listener port, see
# apps/tracking/telematics_service/protocol/teltonika.py.
TELTONIKA_TCP_PORT = settings.TELEMATICS_TELTONIKA_TCP_PORT
TELTONIKA_MAX_FRAME_BYTES = settings.TELEMATICS_TELTONIKA_MAX_FRAME_BYTES
