import pytest
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.tracking.models import TrackingDevice
from apps.tracking.tests.factories import DEFAULT_TEST_SECRET, TrackingDeviceFactory

pytestmark = pytest.mark.django_db

INGEST_URL_NAME = "telemetry-ingest"
VALID_PAYLOAD = {"timestamp": "2026-08-21T10:30:00Z", "latitude": 12.9716, "longitude": 77.5946}


def _auth_header(device, secret=DEFAULT_TEST_SECRET):
    return f"DeviceKey {device.uuid}:{secret}"


class TestDeviceKeyAuthentication:
    def test_valid_key_succeeds(self, client):
        device = TrackingDeviceFactory(status=TrackingDevice.Status.ACTIVE)
        response = client.post(
            reverse(INGEST_URL_NAME), data=VALID_PAYLOAD, content_type="application/json",
            HTTP_AUTHORIZATION=_auth_header(device),
        )
        assert response.status_code in (200, 201, 202)

    def test_missing_header_is_401(self, client):
        response = client.post(reverse(INGEST_URL_NAME), data=VALID_PAYLOAD, content_type="application/json")
        assert response.status_code == 401

    def test_malformed_header_is_401(self, client):
        device = TrackingDeviceFactory()
        response = client.post(
            reverse(INGEST_URL_NAME), data=VALID_PAYLOAD, content_type="application/json",
            HTTP_AUTHORIZATION=f"DeviceKey {device.uuid}-no-colon-secret",
        )
        assert response.status_code == 401

    def test_unknown_device_is_401(self, client):
        response = client.post(
            reverse(INGEST_URL_NAME), data=VALID_PAYLOAD, content_type="application/json",
            HTTP_AUTHORIZATION="DeviceKey 00000000-0000-0000-0000-000000000000:whatever",
        )
        assert response.status_code == 401

    def test_wrong_secret_is_401(self, client):
        device = TrackingDeviceFactory()
        response = client.post(
            reverse(INGEST_URL_NAME), data=VALID_PAYLOAD, content_type="application/json",
            HTTP_AUTHORIZATION=_auth_header(device, secret="wrong-secret"),
        )
        assert response.status_code == 401

    @pytest.mark.parametrize("status", [
        TrackingDevice.Status.SUSPENDED, TrackingDevice.Status.INACTIVE,
        TrackingDevice.Status.FAULTY, TrackingDevice.Status.REMOVED,
    ])
    def test_valid_key_but_inactive_device_is_403(self, client, status):
        device = TrackingDeviceFactory(status=status)
        response = client.post(
            reverse(INGEST_URL_NAME), data=VALID_PAYLOAD, content_type="application/json",
            HTTP_AUTHORIZATION=_auth_header(device),
        )
        assert response.status_code == 403

    def test_device_auth_independent_of_any_user_or_session(self, client):
        """A device request must succeed with zero Django session/login —
        confirms device auth never depends on request.user."""
        device = TrackingDeviceFactory()
        assert client.session.session_key is None or not client.session.get("_auth_user_id")
        response = client.post(
            reverse(INGEST_URL_NAME), data=VALID_PAYLOAD, content_type="application/json",
            HTTP_AUTHORIZATION=_auth_header(device),
        )
        assert response.status_code in (200, 201, 202)

    def test_regular_human_login_does_not_authenticate_ingestion(self, client):
        """A logged-in staff user with no device header must not be able to
        post telemetry — session auth is not part of this endpoint's stack."""
        user = UserFactory()
        client.force_login(user)
        response = client.post(reverse(INGEST_URL_NAME), data=VALID_PAYLOAD, content_type="application/json")
        assert response.status_code == 401
