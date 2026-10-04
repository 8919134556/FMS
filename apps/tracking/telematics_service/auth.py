"""Device identification/authentication for the TCP service.

Deliberately separate from apps.tracking.authentication.DeviceKeyAuthentication
(the existing HTTP device auth): a real GPS/MDVR device speaking a wire
protocol only ever knows its own IMEI, never a Django UUID, so the lookup
key here is necessarily different. The actual verification primitive
(``check_password`` against ``TrackingDevice.secret_hash``) is reused
as-is — this is a second transport into the same credential store, not a
competing auth scheme.
"""

from django.contrib.auth.hashers import check_password

from apps.tracking.models import TrackingDevice


def authenticate_device_sync(imei, secret):
    """Unknown IMEI, wrong secret, and an inactive device all return
    ``None`` identically — never distinguishable to a caller or in logs
    (anti-enumeration). Never logs ``secret``."""
    if not imei or not secret:
        return None
    device = TrackingDevice.objects.filter(imei=imei).first()
    if device is None or not device.secret_hash or not check_password(secret, device.secret_hash):
        return None
    if device.status != TrackingDevice.Status.ACTIVE:
        return None
    return device


def authenticate_teltonika_device_sync(imei):
    """Teltonika's real Codec 8 login packet carries ONLY the IMEI — there
    is no secret/password/token field in the base protocol. This is a
    genuine, documented limitation of the wire protocol itself, not an
    oversight: authentication here is "does an ACTIVE TrackingDevice with
    this IMEI and provider=teltonika exist", full stop. Real deployments
    mitigate IMEI-only auth with network-level controls (private
    APN/VPN, IP allowlisting per SIM) or vendor-specific TLS/certificate
    extensions some Teltonika firmware profiles support — none of which
    are part of the base Codec 8 spec and are out of scope here. Never
    rely on IMEI secrecy alone as a real security boundary."""
    if not imei:
        return None
    return TrackingDevice.objects.filter(
        imei=imei, provider=TrackingDevice.Provider.TELTONIKA, status=TrackingDevice.Status.ACTIVE
    ).first()
