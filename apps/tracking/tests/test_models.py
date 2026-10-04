import datetime

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.tracking.models import TelemetryEvent, TrackingDevice, VehicleCurrentTelemetry
from apps.tracking.tests.factories import TelemetryEventFactory, TrackingDeviceFactory, VehicleCurrentTelemetryFactory
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


class TestTrackingDeviceFields:
    def test_defaults(self):
        device = TrackingDeviceFactory()
        assert device.status == TrackingDevice.Status.ACTIVE
        assert device.provider == TrackingDevice.Provider.GENERIC
        assert device.vehicle is None
        assert device.secret_hash  # factory sets one, but never a raw value

    def test_imei_uniqueness(self):
        TrackingDeviceFactory(imei="123456789012345")
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                TrackingDeviceFactory(imei="123456789012345")

    def test_one_vehicle_per_device(self):
        vehicle = VehicleFactory()
        TrackingDeviceFactory(vehicle=vehicle)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                TrackingDeviceFactory(vehicle=vehicle)


class TestTelemetryEventUniqueness:
    def test_duplicate_device_timestamp_lat_lon_deduped_by_bulk_create(self):
        device = TrackingDeviceFactory()
        now = timezone.now()
        TelemetryEvent.objects.bulk_create(
            [TelemetryEvent(device=device, timestamp=now, latitude="12.971600", longitude="77.594600")]
        )
        TelemetryEvent.objects.bulk_create(
            [TelemetryEvent(device=device, timestamp=now, latitude="12.971600", longitude="77.594600")],
            ignore_conflicts=True,
        )
        assert TelemetryEvent.objects.filter(device=device, timestamp=now).count() == 1

    def test_different_coordinates_not_deduped(self):
        device = TrackingDeviceFactory()
        now = timezone.now()
        TelemetryEvent.objects.bulk_create(
            [
                TelemetryEvent(device=device, timestamp=now, latitude="12.971600", longitude="77.594600"),
                TelemetryEvent(device=device, timestamp=now, latitude="13.000000", longitude="77.594600"),
            ],
            ignore_conflicts=True,
        )
        assert TelemetryEvent.objects.filter(device=device, timestamp=now).count() == 2


class TestVehicleCurrentTelemetryUniqueness:
    def test_one_row_per_vehicle(self):
        vehicle = VehicleFactory()
        VehicleCurrentTelemetryFactory(vehicle=vehicle)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                VehicleCurrentTelemetryFactory(vehicle=vehicle)

    def test_accessible_from_vehicle(self):
        vehicle = VehicleFactory()
        current = VehicleCurrentTelemetryFactory(vehicle=vehicle, speed="45.20")
        vehicle.refresh_from_db()
        assert vehicle.current_telemetry.pk == current.pk
