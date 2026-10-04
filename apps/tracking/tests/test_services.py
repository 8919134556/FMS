import datetime
import decimal

import pytest
from django.contrib.auth.hashers import check_password
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditLog
from apps.tracking import services
from apps.tracking.models import RawTelemetryEvent, TelemetryEvent, TrackingDevice, VehicleCurrentTelemetry
from apps.tracking.providers.base import NormalizedEvent
from apps.tracking.tests.factories import TrackingDeviceFactory, VehicleCurrentTelemetryFactory
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


class TestIssueDeviceKey:
    def test_returns_raw_secret_and_stores_only_hash(self):
        device = TrackingDeviceFactory()
        actor = UserFactory()
        raw_key = services.issue_device_key(device=device, actor=actor)
        device.refresh_from_db()
        assert raw_key not in device.secret_hash
        assert check_password(raw_key, device.secret_hash)

    def test_logs_audit_without_the_secret(self):
        device = TrackingDeviceFactory()
        actor = UserFactory()
        raw_key = services.issue_device_key(device=device, actor=actor)
        log = AuditLog.objects.filter(module="tracking_device", entity_id=str(device.pk)).latest("timestamp")
        assert log.new_value == {"key_issued": True}
        assert raw_key not in str(log.new_value)

    def test_regenerating_invalidates_previous_key(self):
        device = TrackingDeviceFactory()
        actor = UserFactory()
        first_key = services.issue_device_key(device=device, actor=actor)
        second_key = services.issue_device_key(device=device, actor=actor)
        device.refresh_from_db()
        assert not check_password(first_key, device.secret_hash)
        assert check_password(second_key, device.secret_hash)


class TestAssignUnassignDevice:
    def test_assign_sets_vehicle_and_logs(self):
        device = TrackingDeviceFactory()
        vehicle = VehicleFactory()
        actor = UserFactory()
        services.assign_device(device=device, vehicle=vehicle, actor=actor)
        device.refresh_from_db()
        assert device.vehicle_id == vehicle.pk
        assert AuditLog.objects.filter(
            module="tracking_device", entity_id=str(device.pk), action=AuditLog.Action.ASSIGN
        ).exists()

    def test_assign_rejects_vehicle_already_assigned(self):
        vehicle = VehicleFactory()
        TrackingDeviceFactory(vehicle=vehicle)
        other_device = TrackingDeviceFactory()
        actor = UserFactory()
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            services.assign_device(device=other_device, vehicle=vehicle, actor=actor)

    def test_unassign_clears_vehicle_and_logs(self):
        vehicle = VehicleFactory()
        device = TrackingDeviceFactory(vehicle=vehicle)
        actor = UserFactory()
        services.unassign_device(device=device, actor=actor)
        device.refresh_from_db()
        assert device.vehicle_id is None
        assert AuditLog.objects.filter(
            module="tracking_device", entity_id=str(device.pk), action=AuditLog.Action.UNASSIGN
        ).exists()


class TestDisableEnableDevice:
    def test_disable_sets_suspended_and_logs(self):
        device = TrackingDeviceFactory(status=TrackingDevice.Status.ACTIVE)
        actor = UserFactory()
        services.disable_device(device=device, actor=actor)
        device.refresh_from_db()
        assert device.status == TrackingDevice.Status.SUSPENDED
        assert AuditLog.objects.filter(
            module="tracking_device", entity_id=str(device.pk), action=AuditLog.Action.UPDATE
        ).exists()

    def test_enable_restores_active(self):
        device = TrackingDeviceFactory(status=TrackingDevice.Status.SUSPENDED)
        actor = UserFactory()
        services.enable_device(device=device, actor=actor)
        device.refresh_from_db()
        assert device.status == TrackingDevice.Status.ACTIVE


class TestValidateNormalizedEvent:
    def _event(self, **overrides):
        defaults = dict(timestamp=timezone.now(), latitude=12.9716, longitude=77.5946, speed=45)
        defaults.update(overrides)
        return NormalizedEvent(**defaults)

    def test_valid_event_has_no_problems(self):
        assert services.validate_normalized_event(self._event()) == []

    def test_latitude_out_of_range(self):
        problems = services.validate_normalized_event(self._event(latitude=91))
        assert any("latitude" in p for p in problems)

    def test_longitude_out_of_range(self):
        problems = services.validate_normalized_event(self._event(longitude=-181))
        assert any("longitude" in p for p in problems)

    def test_negative_speed_rejected(self):
        problems = services.validate_normalized_event(self._event(speed=-5))
        assert any("speed" in p for p in problems)

    def test_excessive_speed_rejected(self):
        problems = services.validate_normalized_event(self._event(speed=9999))
        assert any("speed" in p for p in problems)

    def test_naive_timestamp_rejected(self):
        problems = services.validate_normalized_event(self._event(timestamp=datetime.datetime(2026, 1, 1)))
        assert any("timezone-aware" in p for p in problems)

    def test_far_future_timestamp_rejected(self):
        problems = services.validate_normalized_event(
            self._event(timestamp=timezone.now() + datetime.timedelta(days=10))
        )
        assert any("future" in p for p in problems)


class TestUpsertCurrentTelemetry:
    def test_creates_when_absent(self):
        vehicle = VehicleFactory()
        device = TrackingDeviceFactory(vehicle=vehicle)
        event = NormalizedEvent(timestamp=timezone.now(), latitude=12.9716, longitude=77.5946, speed=10)
        services.upsert_current_telemetry(vehicle=vehicle, device=device, event=event)
        current = VehicleCurrentTelemetry.objects.get(vehicle=vehicle)
        assert current.speed == 10

    def test_newer_event_overwrites(self):
        vehicle = VehicleFactory()
        device = TrackingDeviceFactory(vehicle=vehicle)
        now = timezone.now()
        VehicleCurrentTelemetryFactory(vehicle=vehicle, device=device, timestamp=now, speed="10.00")
        newer_event = NormalizedEvent(
            timestamp=now + datetime.timedelta(minutes=5), latitude=13.0, longitude=78.0, speed=50
        )
        services.upsert_current_telemetry(vehicle=vehicle, device=device, event=newer_event)
        current = VehicleCurrentTelemetry.objects.get(vehicle=vehicle)
        assert current.speed == 50
        assert current.timestamp == newer_event.timestamp

    def test_older_event_does_not_overwrite(self):
        """Out-of-order data must never regress the current position."""
        vehicle = VehicleFactory()
        device = TrackingDeviceFactory(vehicle=vehicle)
        now = timezone.now()
        VehicleCurrentTelemetryFactory(vehicle=vehicle, device=device, timestamp=now, speed="60.00")
        older_event = NormalizedEvent(
            timestamp=now - datetime.timedelta(minutes=5), latitude=1.0, longitude=1.0, speed=5
        )
        services.upsert_current_telemetry(vehicle=vehicle, device=device, event=older_event)
        current = VehicleCurrentTelemetry.objects.get(vehicle=vehicle)
        assert current.speed == 60
        assert current.timestamp == now

    def test_equal_timestamp_does_not_overwrite(self):
        vehicle = VehicleFactory()
        device = TrackingDeviceFactory(vehicle=vehicle)
        now = timezone.now()
        VehicleCurrentTelemetryFactory(vehicle=vehicle, device=device, timestamp=now, speed="60.00")
        same_time_event = NormalizedEvent(timestamp=now, latitude=1.0, longitude=1.0, speed=5)
        services.upsert_current_telemetry(vehicle=vehicle, device=device, event=same_time_event)
        current = VehicleCurrentTelemetry.objects.get(vehicle=vehicle)
        assert current.speed == 60


class TestConnectionStatusFor:
    def test_none_is_offline(self):
        assert services.connection_status_for(None) == "OFFLINE"

    def test_recent_is_online(self, settings):
        settings.TELEMATICS_ONLINE_THRESHOLD_MINUTES = 5
        settings.TELEMATICS_OFFLINE_THRESHOLD_MINUTES = 30
        vehicle = VehicleFactory()
        current = VehicleCurrentTelemetryFactory(vehicle=vehicle, timestamp=timezone.now())
        assert services.connection_status_for(current) == "ONLINE"

    def test_moderately_stale_is_stale(self, settings):
        settings.TELEMATICS_ONLINE_THRESHOLD_MINUTES = 5
        settings.TELEMATICS_OFFLINE_THRESHOLD_MINUTES = 30
        vehicle = VehicleFactory()
        current = VehicleCurrentTelemetryFactory(
            vehicle=vehicle, timestamp=timezone.now() - datetime.timedelta(minutes=15)
        )
        assert services.connection_status_for(current) == "STALE"

    def test_very_stale_is_offline(self, settings):
        settings.TELEMATICS_ONLINE_THRESHOLD_MINUTES = 5
        settings.TELEMATICS_OFFLINE_THRESHOLD_MINUTES = 30
        vehicle = VehicleFactory()
        current = VehicleCurrentTelemetryFactory(
            vehicle=vehicle, timestamp=timezone.now() - datetime.timedelta(hours=2)
        )
        assert services.connection_status_for(current) == "OFFLINE"


class TestTelemetryIngestionService:
    def _generic_device(self, **kwargs):
        kwargs.setdefault("provider", TrackingDevice.Provider.GENERIC)
        return TrackingDeviceFactory(**kwargs)

    def test_single_event_ingested(self):
        vehicle = VehicleFactory()
        device = self._generic_device(vehicle=vehicle)
        payload = {"timestamp": "2026-08-21T10:30:00Z", "latitude": 12.9716, "longitude": 77.5946, "speed": 45.2}
        result = services.TelemetryIngestionService.ingest(device=device, raw_payload=payload)
        assert result.accepted == 1
        assert result.rejected == 0
        assert TelemetryEvent.objects.filter(device=device).count() == 1
        assert VehicleCurrentTelemetry.objects.filter(vehicle=vehicle).exists()

    def test_ingest_evaluates_geofences_for_the_vehicle(self):
        from apps.geofences.models import GeofenceEvent
        from apps.geofences.tests.factories import GeofenceFactory

        vehicle = VehicleFactory()
        device = self._generic_device(vehicle=vehicle)
        GeofenceFactory(center_latitude="12.971600", center_longitude="77.594600", radius_meters=500)
        payload = {"timestamp": "2026-08-21T10:30:00Z", "latitude": 12.9716, "longitude": 77.5946, "speed": 0}
        services.TelemetryIngestionService.ingest(device=device, raw_payload=payload)
        assert GeofenceEvent.objects.filter(vehicle=vehicle, event_type=GeofenceEvent.EventType.ENTER).exists()

    def test_ingest_still_succeeds_if_geofence_evaluation_raises(self, monkeypatch):
        """Geofencing is additive — a bug there must never break ingestion,
        the one thing this service exists to do reliably."""

        def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr("apps.geofences.services.evaluate_position", _boom)
        vehicle = VehicleFactory()
        device = self._generic_device(vehicle=vehicle)
        payload = {"timestamp": "2026-08-21T10:30:00Z", "latitude": 12.9716, "longitude": 77.5946, "speed": 0}
        result = services.TelemetryIngestionService.ingest(device=device, raw_payload=payload)
        assert result.accepted == 1
        assert VehicleCurrentTelemetry.objects.filter(vehicle=vehicle).exists()

    def test_batch_events_ingested(self):
        vehicle = VehicleFactory()
        device = self._generic_device(vehicle=vehicle)
        payload = {
            "events": [
                {"timestamp": "2026-08-21T10:00:00Z", "latitude": 12.0, "longitude": 77.0},
                {"timestamp": "2026-08-21T10:05:00Z", "latitude": 12.1, "longitude": 77.1},
            ]
        }
        result = services.TelemetryIngestionService.ingest(device=device, raw_payload=payload)
        assert result.accepted == 2
        assert TelemetryEvent.objects.filter(device=device).count() == 2
        current = VehicleCurrentTelemetry.objects.get(vehicle=vehicle)
        assert str(current.latitude) == "12.100000"

    def test_partial_batch_mixed_valid_invalid(self):
        vehicle = VehicleFactory()
        device = self._generic_device(vehicle=vehicle)
        payload = {
            "events": [
                {"timestamp": "2026-08-21T10:00:00Z", "latitude": 12.0, "longitude": 77.0},
                {"timestamp": "2026-08-21T10:05:00Z", "latitude": 999, "longitude": 77.1},
            ]
        }
        result = services.TelemetryIngestionService.ingest(device=device, raw_payload=payload)
        assert result.accepted == 1
        assert result.rejected == 1
        raw = RawTelemetryEvent.objects.filter(device=device).latest("received_at")
        assert raw.processing_status == RawTelemetryEvent.ProcessingStatus.PARTIAL

    def test_malformed_payload_rejected_but_raw_event_stored(self):
        device = self._generic_device()
        payload = {"events": "not-a-list"}
        result = services.TelemetryIngestionService.ingest(device=device, raw_payload=payload)
        assert result.accepted == 0
        assert result.rejected == 1
        assert RawTelemetryEvent.objects.filter(device=device).exists()

    def test_invalid_coordinates_rejected(self):
        device = self._generic_device()
        payload = {"timestamp": "2026-08-21T10:30:00Z", "latitude": 999, "longitude": 77.5946}
        result = services.TelemetryIngestionService.ingest(device=device, raw_payload=payload)
        assert result.accepted == 0
        assert result.rejected == 1
        assert TelemetryEvent.objects.filter(device=device).count() == 0

    def test_invalid_speed_rejected(self):
        device = self._generic_device()
        payload = {"timestamp": "2026-08-21T10:30:00Z", "latitude": 12.9716, "longitude": 77.5946, "speed": -10}
        result = services.TelemetryIngestionService.ingest(device=device, raw_payload=payload)
        assert result.accepted == 0
        assert result.rejected == 1

    def test_unsupported_provider_fails_cleanly(self):
        device = self._generic_device(provider=TrackingDevice.Provider.TELTONIKA)
        payload = {"timestamp": "2026-08-21T10:30:00Z", "latitude": 12.9716, "longitude": 77.5946}
        result = services.TelemetryIngestionService.ingest(device=device, raw_payload=payload)
        assert result.accepted == 0
        assert result.rejected == 1
        raw = RawTelemetryEvent.objects.filter(device=device).latest("received_at")
        assert raw.processing_status == RawTelemetryEvent.ProcessingStatus.FAILED

    def test_unassigned_device_stores_history_without_current_state(self):
        device = self._generic_device(vehicle=None)
        payload = {"timestamp": "2026-08-21T10:30:00Z", "latitude": 12.9716, "longitude": 77.5946}
        result = services.TelemetryIngestionService.ingest(device=device, raw_payload=payload)
        assert result.accepted == 1
        assert TelemetryEvent.objects.filter(device=device).count() == 1
        assert VehicleCurrentTelemetry.objects.count() == 0

    def test_vehicle_id_in_payload_is_ignored(self):
        """Anti-spoofing: vehicle resolution comes from the authenticated
        device, never from the request body."""
        real_vehicle = VehicleFactory()
        other_vehicle = VehicleFactory()
        device = self._generic_device(vehicle=real_vehicle)
        payload = {
            "timestamp": "2026-08-21T10:30:00Z", "latitude": 12.9716, "longitude": 77.5946,
            "vehicle": str(other_vehicle.uuid), "vehicle_id": other_vehicle.pk,
        }
        services.TelemetryIngestionService.ingest(device=device, raw_payload=payload)
        event = TelemetryEvent.objects.get(device=device)
        assert event.vehicle_id == real_vehicle.pk

    def test_updates_device_last_communication(self):
        device = self._generic_device()
        assert device.last_communication is None
        payload = {"timestamp": "2026-08-21T10:30:00Z", "latitude": 12.9716, "longitude": 77.5946}
        services.TelemetryIngestionService.ingest(device=device, raw_payload=payload)
        device.refresh_from_db()
        assert device.last_communication is not None

    def test_ingestion_creates_zero_audit_log_rows(self):
        device = self._generic_device()
        payload = {"events": [{"timestamp": "2026-08-21T10:30:00Z", "latitude": 12.9716, "longitude": 77.5946}] * 20}
        before = AuditLog.objects.count()
        services.TelemetryIngestionService.ingest(device=device, raw_payload=payload)
        assert AuditLog.objects.count() == before

    def test_duplicate_retry_does_not_create_duplicate_rows(self):
        device = self._generic_device()
        payload = {"timestamp": "2026-08-21T10:30:00Z", "latitude": 12.9716, "longitude": 77.5946}
        services.TelemetryIngestionService.ingest(device=device, raw_payload=payload)
        services.TelemetryIngestionService.ingest(device=device, raw_payload=payload)
        assert TelemetryEvent.objects.filter(device=device).count() == 1


class TestMovementStateFor:
    def test_none_current_returns_none(self):
        assert services.movement_state_for(None) is None

    def test_none_speed_returns_none(self):
        vehicle = VehicleFactory()
        current = VehicleCurrentTelemetryFactory(vehicle=vehicle, speed=None)
        assert services.movement_state_for(current) is None

    def test_speed_above_threshold_is_moving(self, settings):
        settings.TELEMATICS_MOVEMENT_SPEED_THRESHOLD_KMH = 5
        vehicle = VehicleFactory()
        current = VehicleCurrentTelemetryFactory(vehicle=vehicle, speed=decimal.Decimal("10.00"))
        assert services.movement_state_for(current) == "MOVING"

    def test_speed_at_or_below_threshold_is_idle(self, settings):
        settings.TELEMATICS_MOVEMENT_SPEED_THRESHOLD_KMH = 5
        vehicle = VehicleFactory()
        current = VehicleCurrentTelemetryFactory(vehicle=vehicle, speed=decimal.Decimal("5.00"))
        assert services.movement_state_for(current) == "IDLE"

        vehicle2 = VehicleFactory()
        current2 = VehicleCurrentTelemetryFactory(vehicle=vehicle2, speed=decimal.Decimal("0.00"))
        assert services.movement_state_for(current2) == "IDLE"


class TestFleetCurrentTelemetry:
    def test_only_returns_vehicles_with_telemetry(self):
        with_telemetry = VehicleFactory()
        VehicleCurrentTelemetryFactory(vehicle=with_telemetry)
        VehicleFactory()  # no telemetry — must never appear

        results = services.fleet_current_telemetry()
        uuids = [r["vehicle_uuid"] for r in results]
        assert uuids == [with_telemetry.uuid]

    def test_includes_active_trip_when_present(self):
        from apps.trips.models import Trip
        from apps.trips.tests.factories import TripFactory

        vehicle = VehicleFactory()
        VehicleCurrentTelemetryFactory(vehicle=vehicle)
        trip = TripFactory(vehicle=vehicle, status=Trip.Status.IN_PROGRESS)

        results = services.fleet_current_telemetry()
        assert results[0]["active_trip"] == {"uuid": str(trip.uuid), "trip_number": trip.trip_number}

    def test_active_trip_is_none_when_absent(self):
        vehicle = VehicleFactory()
        VehicleCurrentTelemetryFactory(vehicle=vehicle)
        results = services.fleet_current_telemetry()
        assert results[0]["active_trip"] is None

    def test_completed_trip_is_not_active(self):
        from apps.trips.models import Trip
        from apps.trips.tests.factories import TripFactory

        vehicle = VehicleFactory()
        VehicleCurrentTelemetryFactory(vehicle=vehicle)
        TripFactory(vehicle=vehicle, status=Trip.Status.COMPLETED)

        results = services.fleet_current_telemetry()
        assert results[0]["active_trip"] is None

    def test_includes_vehicle_context_fields(self):
        from apps.clients.tests.factories import ClientFactory
        from apps.vehicles.tests.factories import VehicleTypeFactory

        client = ClientFactory()
        vehicle_type = VehicleTypeFactory()
        vehicle = VehicleFactory(client=client, vehicle_type=vehicle_type)
        VehicleCurrentTelemetryFactory(vehicle=vehicle)

        result = services.fleet_current_telemetry()[0]
        assert result["client"] == client.client_name
        assert result["vehicle_type"] == vehicle_type.name
        assert result["availability_status"] == vehicle.availability_status

    def test_connection_status_and_movement_state_present(self):
        vehicle = VehicleFactory()
        VehicleCurrentTelemetryFactory(vehicle=vehicle, speed="42.00", timestamp=timezone.now())
        result = services.fleet_current_telemetry()[0]
        assert result["connection_status"] == "ONLINE"
        assert result["movement_state"] == "MOVING"

    def test_driver_name_from_current_driver_when_assigned(self):
        from apps.drivers.tests.factories import DriverFactory

        driver = DriverFactory(first_name="Vikram", last_name="Raj")
        vehicle = VehicleFactory(current_driver=driver)
        VehicleCurrentTelemetryFactory(vehicle=vehicle)
        result = services.fleet_current_telemetry()[0]
        assert result["driver_name"] == "Vikram Raj"

    def test_driver_name_is_none_when_unassigned(self):
        vehicle = VehicleFactory()
        VehicleCurrentTelemetryFactory(vehicle=vehicle)
        result = services.fleet_current_telemetry()[0]
        assert result["driver_name"] is None


class TestFleetConnectivityCounts:
    def test_matches_connection_status_for_thresholds(self, settings):
        settings.TELEMATICS_ONLINE_THRESHOLD_MINUTES = 5
        settings.TELEMATICS_OFFLINE_THRESHOLD_MINUTES = 30
        now = timezone.now()

        online_vehicle = VehicleFactory()
        VehicleCurrentTelemetryFactory(vehicle=online_vehicle, timestamp=now)

        stale_vehicle = VehicleFactory()
        VehicleCurrentTelemetryFactory(vehicle=stale_vehicle, timestamp=now - datetime.timedelta(minutes=15))

        offline_vehicle = VehicleFactory()
        VehicleCurrentTelemetryFactory(vehicle=offline_vehicle, timestamp=now - datetime.timedelta(hours=2))

        no_telemetry_vehicle = VehicleFactory()

        counts = services.fleet_connectivity_counts()
        assert counts["online"] == 1
        assert counts["stale"] == 1
        assert counts["offline"] == 1
        assert counts["no_telemetry"] == 1
        assert counts["total_tracked"] == 3

        # Cross-check against connection_status_for directly — the two must
        # never drift apart since they share the same threshold settings.
        assert services.connection_status_for(
            VehicleCurrentTelemetry.objects.get(vehicle=online_vehicle)
        ) == "ONLINE"
        assert services.connection_status_for(
            VehicleCurrentTelemetry.objects.get(vehicle=stale_vehicle)
        ) == "STALE"
        assert services.connection_status_for(
            VehicleCurrentTelemetry.objects.get(vehicle=offline_vehicle)
        ) == "OFFLINE"

    def test_no_vehicles_returns_zeroes(self):
        counts = services.fleet_connectivity_counts()
        assert counts == {"total_tracked": 0, "online": 0, "stale": 0, "offline": 0, "no_telemetry": 0}
