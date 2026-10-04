import datetime

import pytest
from django.utils import timezone

from apps.drivers.models import Driver
from apps.drivers.tests.factories import DriverFactory

pytestmark = pytest.mark.django_db


class TestLicenseExpiryStatus:
    def test_no_expiry_date_returns_none(self):
        driver = DriverFactory(license_expiry_date=None)
        assert driver.license_expiry_status is None

    def test_future_date_beyond_warning_window_is_valid(self):
        future = timezone.now().date() + datetime.timedelta(days=Driver.LICENSE_EXPIRY_WARNING_DAYS + 5)
        driver = DriverFactory(license_expiry_date=future)
        assert driver.license_expiry_status == "VALID"

    def test_date_within_warning_window_is_expiring_soon(self):
        soon = timezone.now().date() + datetime.timedelta(days=Driver.LICENSE_EXPIRY_WARNING_DAYS - 5)
        driver = DriverFactory(license_expiry_date=soon)
        assert driver.license_expiry_status == "EXPIRING_SOON"

    def test_date_exactly_at_warning_boundary_is_expiring_soon(self):
        boundary = timezone.now().date() + datetime.timedelta(days=Driver.LICENSE_EXPIRY_WARNING_DAYS)
        driver = DriverFactory(license_expiry_date=boundary)
        assert driver.license_expiry_status == "EXPIRING_SOON"

    def test_past_date_is_expired(self):
        past = timezone.now().date() - datetime.timedelta(days=1)
        driver = DriverFactory(license_expiry_date=past)
        assert driver.license_expiry_status == "EXPIRED"


class TestDriverUniqueness:
    def test_duplicate_employee_id_rejected(self):
        from django.db import IntegrityError, transaction

        DriverFactory(employee_id="DUPTEST01")
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                DriverFactory(employee_id="DUPTEST01")

    def test_duplicate_license_number_rejected(self):
        from django.db import IntegrityError, transaction

        DriverFactory(license_number="LICDUP01")
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                DriverFactory(license_number="LICDUP01")

    def test_duplicate_mobile_number_rejected(self):
        from django.db import IntegrityError, transaction

        DriverFactory(mobile_number="+15557778888")
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                DriverFactory(mobile_number="+15557778888")


class TestGetFullName:
    def test_full_name_skips_blank_middle_name(self):
        driver = DriverFactory(first_name="Ramesh", middle_name="", last_name="Kumar")
        assert driver.get_full_name() == "Ramesh Kumar"

    def test_full_name_includes_middle_name(self):
        driver = DriverFactory(first_name="Ramesh", middle_name="B", last_name="Kumar")
        assert driver.get_full_name() == "Ramesh B Kumar"
