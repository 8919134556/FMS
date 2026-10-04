"""Device (machine-to-machine) authentication for telemetry ingestion.

Deliberately separate from ``apps.core.permissions``/``apps.core.api_permissions``
— those assume ``request.user`` is a real, session-authenticated staff
``User`` with a ``.role``. A GPS device has neither, so it authenticates via
a per-device shared secret sent in the ``Authorization`` header (never a
query string) and is represented as ``request.auth`` (a ``TrackingDevice``),
not ``request.user`` (left as ``AnonymousUser``).
"""

from django.contrib.auth.hashers import check_password
from django.contrib.auth.models import AnonymousUser
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication
from rest_framework.permissions import BasePermission
from rest_framework.throttling import SimpleRateThrottle, UserRateThrottle

from apps.tracking.models import TrackingDevice

DEVICE_AUTH_KEYWORD = "DeviceKey"


class DeviceKeyAuthentication(BaseAuthentication):
    """``Authorization: DeviceKey <device_uuid>:<raw_secret>``.

    Returns ``None`` (no attempt) when the header is absent/not ours, so
    other authenticators in the chain still get a chance. Raises
    ``AuthenticationFailed`` (-> 401) for anything that IS an attempt but is
    invalid — unknown device, wrong secret, malformed header. Whether the
    device is currently ACTIVE is a *permission* concern (``IsActiveDevice``,
    -> 403), not an authentication one — keeping them separate is what lets
    DRF report 401 vs 403 correctly (see ``IsActiveDevice`` docstring).
    """

    keyword = DEVICE_AUTH_KEYWORD

    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.startswith(f"{self.keyword} "):
            return None

        raw = header[len(self.keyword) + 1 :].strip()
        if ":" not in raw:
            raise exceptions.AuthenticationFailed("Malformed device credentials.")
        device_uuid, raw_secret = raw.split(":", 1)

        device = TrackingDevice.objects.filter(uuid=device_uuid).first()
        if device is None or not device.secret_hash or not check_password(raw_secret, device.secret_hash):
            raise exceptions.AuthenticationFailed("Invalid device credentials.")

        return (AnonymousUser(), device)

    def authenticate_header(self, request):
        return self.keyword


class IsActiveDevice(BasePermission):
    """Only an ACTIVE device may ingest.

    A device that authenticates successfully (correct key) but is
    SUSPENDED/INACTIVE/FAULTY/REMOVED fails *this* check, which DRF reports
    as 403 (not 401) because ``request.successful_authenticator`` was
    already set by ``DeviceKeyAuthentication`` — standard
    ``APIView.permission_denied`` behavior, not custom logic.
    """

    message = "This device is not active."

    def has_permission(self, request, view):
        device = request.auth
        return bool(device) and getattr(device, "status", None) == TrackingDevice.Status.ACTIVE


class FleetReadRateThrottle(UserRateThrottle):
    """The Live Tracking page polls the fleet feed as often as every 5 s
    (720 requests/hour for one open tab). That would exhaust the general
    per-user API budget (``user``: 1000/hour) after ~1.4 tabs and lock the user
    out of every other endpoint, so this read-only, two-query feed gets its own
    bucket instead of sharing the default one."""

    scope = "fleet_read"


class DeviceIngestRateThrottle(SimpleRateThrottle):
    """Per-device (not per-user) ingestion throttle — every device is
    ``AnonymousUser``, so the default ``UserRateThrottle`` would incorrectly
    lump every device's traffic into one shared anonymous bucket."""

    scope = "device_ingest"

    def get_cache_key(self, request, view):
        device = getattr(request, "auth", None)
        if not device:
            return None
        return self.cache_format % {"scope": self.scope, "ident": device.uuid}
