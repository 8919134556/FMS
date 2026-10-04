import factory
from django.contrib.auth.hashers import make_password
from django.utils import timezone
from factory.django import DjangoModelFactory

from apps.tracking.models import (
    DeviceCommand,
    RawTelemetryEvent,
    TelemetryEvent,
    TrackingDevice,
    VehicleCurrentTelemetry,
)

# Known raw secret paired with every TrackingDeviceFactory-created device's
# secret_hash by default — lets auth tests build a valid Authorization
# header without going through the issue_device_key reveal flow.
DEFAULT_TEST_SECRET = "test-device-secret"


class TrackingDeviceFactory(DjangoModelFactory):
    class Meta:
        model = TrackingDevice

    imei = factory.Sequence(lambda n: f"860000000{n:06d}")
    name = factory.Sequence(lambda n: f"Device {n}")
    provider = TrackingDevice.Provider.GENERIC
    status = TrackingDevice.Status.ACTIVE
    secret_hash = factory.LazyFunction(lambda: make_password(DEFAULT_TEST_SECRET))


class RawTelemetryEventFactory(DjangoModelFactory):
    class Meta:
        model = RawTelemetryEvent

    device = factory.SubFactory(TrackingDeviceFactory)
    provider = TrackingDevice.Provider.GENERIC
    payload = factory.LazyFunction(dict)
    processing_status = RawTelemetryEvent.ProcessingStatus.PROCESSED


class TelemetryEventFactory(DjangoModelFactory):
    class Meta:
        model = TelemetryEvent

    device = factory.SubFactory(TrackingDeviceFactory)
    timestamp = factory.LazyFunction(timezone.now)
    latitude = "12.971600"
    longitude = "77.594600"


class VehicleCurrentTelemetryFactory(DjangoModelFactory):
    class Meta:
        model = VehicleCurrentTelemetry

    device = factory.SubFactory(TrackingDeviceFactory)
    timestamp = factory.LazyFunction(timezone.now)
    latitude = "12.971600"
    longitude = "77.594600"


class DeviceCommandFactory(DjangoModelFactory):
    class Meta:
        model = DeviceCommand

    device = factory.SubFactory(TrackingDeviceFactory)
    command_type = DeviceCommand.CommandType.PING
    status = DeviceCommand.Status.PENDING
