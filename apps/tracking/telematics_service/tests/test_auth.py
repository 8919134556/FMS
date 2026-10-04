import logging

import pytest

from apps.tracking.models import TrackingDevice
from apps.tracking.telematics_service.auth import authenticate_device_sync
from apps.tracking.tests.factories import DEFAULT_TEST_SECRET, TrackingDeviceFactory

pytestmark = pytest.mark.django_db


class TestAuthenticateDeviceSync:
    def test_valid_device_authenticates(self):
        device = TrackingDeviceFactory()
        result = authenticate_device_sync(device.imei, DEFAULT_TEST_SECRET)
        assert result is not None
        assert result.pk == device.pk

    def test_unknown_imei_returns_none(self):
        assert authenticate_device_sync("000000000000000", DEFAULT_TEST_SECRET) is None

    def test_wrong_secret_returns_none(self):
        device = TrackingDeviceFactory()
        assert authenticate_device_sync(device.imei, "wrong-secret") is None

    def test_inactive_device_returns_none(self):
        device = TrackingDeviceFactory(status=TrackingDevice.Status.SUSPENDED)
        assert authenticate_device_sync(device.imei, DEFAULT_TEST_SECRET) is None

    def test_missing_credentials_return_none(self):
        assert authenticate_device_sync("", "") is None
        assert authenticate_device_sync(None, None) is None

    def test_all_failure_modes_return_identical_none(self):
        """Anti-enumeration: unknown IMEI, wrong secret, and inactive
        device must be indistinguishable to any caller."""
        device = TrackingDeviceFactory(status=TrackingDevice.Status.SUSPENDED)
        results = {
            authenticate_device_sync("000000000000000", DEFAULT_TEST_SECRET),
            authenticate_device_sync(device.imei, "wrong-secret"),
            authenticate_device_sync(device.imei, DEFAULT_TEST_SECRET),  # inactive
        }
        assert results == {None}

    def test_failed_identification_never_logs_the_secret(self, caplog):
        device = TrackingDeviceFactory()
        secret = "super-secret-value-should-never-appear"
        with caplog.at_level(logging.DEBUG):
            authenticate_device_sync(device.imei, secret)
        for record in caplog.records:
            assert secret not in record.getMessage()
