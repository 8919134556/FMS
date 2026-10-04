import json

import pytest
from django.urls import reverse
from rest_framework.test import APIRequestFactory

from apps.audit.models import AuditLog
from apps.tracking.authentication import DeviceIngestRateThrottle
from apps.tracking.models import TelemetryEvent
from apps.tracking.tests.factories import DEFAULT_TEST_SECRET, TrackingDeviceFactory
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db

INGEST_URL_NAME = "telemetry-ingest"


def _auth_header(device):
    return f"DeviceKey {device.uuid}:{DEFAULT_TEST_SECRET}"


class TestIngestSingleEvent:
    def test_single_event_returns_201(self, client):
        vehicle = VehicleFactory()
        device = TrackingDeviceFactory(vehicle=vehicle)
        payload = {"timestamp": "2026-08-21T10:30:00Z", "latitude": 12.9716, "longitude": 77.5946, "speed": 45.2}
        response = client.post(
            reverse(INGEST_URL_NAME), data=json.dumps(payload), content_type="application/json",
            HTTP_AUTHORIZATION=_auth_header(device),
        )
        assert response.status_code == 201
        body = response.json()
        assert body["accepted"] == 1
        assert body["rejected"] == 0
        assert TelemetryEvent.objects.filter(device=device).count() == 1


class TestIngestBatch:
    def test_batch_returns_201(self, client):
        device = TrackingDeviceFactory()
        payload = {
            "events": [
                {"timestamp": "2026-08-21T10:00:00Z", "latitude": 12.0, "longitude": 77.0},
                {"timestamp": "2026-08-21T10:05:00Z", "latitude": 12.1, "longitude": 77.1},
                {"timestamp": "2026-08-21T10:10:00Z", "latitude": 12.2, "longitude": 77.2},
            ]
        }
        response = client.post(
            reverse(INGEST_URL_NAME), data=json.dumps(payload), content_type="application/json",
            HTTP_AUTHORIZATION=_auth_header(device),
        )
        assert response.status_code == 201
        assert response.json()["accepted"] == 3

    def test_partial_batch_returns_202(self, client):
        device = TrackingDeviceFactory()
        payload = {
            "events": [
                {"timestamp": "2026-08-21T10:00:00Z", "latitude": 12.0, "longitude": 77.0},
                {"timestamp": "not-a-timestamp", "latitude": 12.1, "longitude": 77.1},
            ]
        }
        response = client.post(
            reverse(INGEST_URL_NAME), data=json.dumps(payload), content_type="application/json",
            HTTP_AUTHORIZATION=_auth_header(device),
        )
        assert response.status_code == 202
        body = response.json()
        assert body["accepted"] == 1
        assert body["rejected"] == 1

    def test_fully_malformed_batch_returns_400(self, client):
        device = TrackingDeviceFactory()
        payload = {"events": [{"latitude": 999, "longitude": 77.0}]}  # missing timestamp, bad lat
        response = client.post(
            reverse(INGEST_URL_NAME), data=json.dumps(payload), content_type="application/json",
            HTTP_AUTHORIZATION=_auth_header(device),
        )
        assert response.status_code == 400
        assert response.json()["accepted"] == 0


class TestIngestVehicleResolution:
    def test_payload_vehicle_id_never_trusted(self, client):
        real_vehicle = VehicleFactory()
        spoofed_vehicle = VehicleFactory()
        device = TrackingDeviceFactory(vehicle=real_vehicle)
        payload = {
            "timestamp": "2026-08-21T10:30:00Z", "latitude": 12.9716, "longitude": 77.5946,
            "vehicle": str(spoofed_vehicle.uuid),
        }
        client.post(
            reverse(INGEST_URL_NAME), data=json.dumps(payload), content_type="application/json",
            HTTP_AUTHORIZATION=_auth_header(device),
        )
        event = TelemetryEvent.objects.get(device=device)
        assert event.vehicle_id == real_vehicle.pk
        assert event.vehicle_id != spoofed_vehicle.pk


class TestIngestAudit:
    def test_large_batch_produces_zero_audit_rows(self, client):
        device = TrackingDeviceFactory()
        events = [
            {"timestamp": f"2026-08-21T10:{i:02d}:00Z", "latitude": 12.0 + i * 0.001, "longitude": 77.0}
            for i in range(50)
        ]
        before = AuditLog.objects.count()
        response = client.post(
            reverse(INGEST_URL_NAME), data=json.dumps({"events": events}), content_type="application/json",
            HTTP_AUTHORIZATION=_auth_header(device),
        )
        assert response.status_code == 201
        assert AuditLog.objects.count() == before


class TestDeviceIngestThrottleIsolation:
    def test_cache_key_differs_per_device(self):
        """Unit-level check that the throttle buckets by device.uuid, not by
        request.user (every device is AnonymousUser) — the mechanism that
        makes per-device rate isolation possible without issuing 120+ real
        requests in a test."""
        device_a = TrackingDeviceFactory()
        device_b = TrackingDeviceFactory()
        factory = APIRequestFactory()

        throttle = DeviceIngestRateThrottle()
        request_a = factory.post("/api/telematics/ingest/")
        request_a.auth = device_a
        request_b = factory.post("/api/telematics/ingest/")
        request_b.auth = device_b

        key_a = throttle.get_cache_key(request_a, None)
        key_b = throttle.get_cache_key(request_b, None)
        assert key_a != key_b
        assert str(device_a.uuid) in key_a
        assert str(device_b.uuid) in key_b
