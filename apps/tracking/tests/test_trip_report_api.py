"""Trip Report page + API: permission gating, date-range resolution, and the
JSON shape the frontend (static/js/trip_report.js) depends on."""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Permission
from apps.accounts.tests.factories import UserFactory
from apps.clients.tests.factories import ClientFactory
from apps.core.tests.factories import role_with
from apps.tracking.tests.factories import TrackingDeviceFactory, TelemetryEventFactory
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


def _view_role():
    return role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW))


def _vehicle_with_device(client_obj=None):
    vehicle = VehicleFactory(client=client_obj)
    TrackingDeviceFactory(vehicle=vehicle)
    return vehicle


def _departure(vehicle, client_obj, when, odometer=None):
    """Ignition ON followed by genuine movement (3 readings at speed, ~55 m
    apart) — what a trip start looks like now that ignition ON alone is only
    a candidate (see apps.tracking.trip_report)."""
    for step in range(3):
        TelemetryEventFactory(
            device=vehicle.tracking_device, vehicle=vehicle, client=client_obj, ignition=True, speed=30,
            odometer=odometer, timestamp=when + datetime.timedelta(seconds=5 * step),
            latitude=f"{12.9716 + 0.0005 * step:.6f}", longitude="77.594600",
        )


class TestPagePermissions:
    def test_anonymous_redirected(self, client):
        assert client.get(reverse("tracking:trip_report")).status_code == 302

    def test_requires_tracking_device_view_permission(self, client):
        client.force_login(UserFactory(role=None))
        assert client.get(reverse("tracking:trip_report")).status_code == 403

    def test_viewer_with_permission_gets_the_page(self, client):
        client.force_login(UserFactory(role=_view_role()))
        response = client.get(reverse("tracking:trip_report"))
        assert response.status_code == 200
        assert b"trip_report.js" in response.content

    def test_page_renders_no_trip_data_server_side(self, client):
        """Same discipline as Live Tracking: the shell has no coordinates baked in."""
        client_obj = ClientFactory()
        vehicle = _vehicle_with_device(client_obj)
        TelemetryEventFactory(
            device=vehicle.tracking_device, vehicle=vehicle, client=client_obj,
            ignition=True, latitude="12.999999", longitude="77.999999",
        )
        client.force_login(UserFactory(role=_view_role()))
        response = client.get(reverse("tracking:trip_report"))
        assert b"12.999999" not in response.content


class TestDataAPI:
    def test_requires_authentication(self, client):
        assert client.get(reverse("trip-report-data")).status_code in (401, 403)

    def test_requires_permission(self, client):
        client.force_login(UserFactory(role=None))
        assert client.get(reverse("trip-report-data")).status_code == 403

    def test_default_range_is_today_and_shape_matches_the_frontend_contract(self, client):
        client_obj = ClientFactory()
        vehicle = _vehicle_with_device(client_obj)
        now = timezone.now()
        _departure(vehicle, client_obj, now - datetime.timedelta(minutes=10), odometer=100)
        client.force_login(UserFactory(role=_view_role()))

        response = client.get(reverse("trip-report-data"))
        assert response.status_code == 200
        body = response.json()
        assert body["range"]["key"] == "today"
        assert body["count"] == 1
        assert body["summary"]["total_trips"] == 1 and body["summary"]["active_trips"] == 1
        trip = body["results"][0]
        for key in (
            "vehicle_uuid", "registration_number", "vehicle_type", "driver_name", "status", "ignition",
            "start_time", "end_time", "duration_seconds", "start_location", "end_location",
            "start_latitude", "start_longitude", "end_latitude", "end_longitude", "distance_km",
        ):
            assert key in trip
        assert trip["vehicle_uuid"] == str(vehicle.uuid)
        assert trip["status"] == "ACTIVE"
        assert trip["end_time"] is None

    def test_yesterday_range(self, client):
        client_obj = ClientFactory()
        vehicle = _vehicle_with_device(client_obj)
        now = timezone.now()
        yesterday = now - datetime.timedelta(days=1)
        TelemetryEventFactory(
            device=vehicle.tracking_device, vehicle=vehicle, client=client_obj,
            timestamp=yesterday, ignition=True, odometer=100,
        )
        TelemetryEventFactory(
            device=vehicle.tracking_device, vehicle=vehicle, client=client_obj,
            timestamp=yesterday + datetime.timedelta(minutes=20), ignition=False, odometer=110,
        )
        client.force_login(UserFactory(role=_view_role()))

        today_response = client.get(reverse("trip-report-data"), {"range": "today"})
        assert today_response.json()["count"] == 0

        yday_response = client.get(reverse("trip-report-data"), {"range": "yesterday"})
        body = yday_response.json()
        assert body["count"] == 1
        assert body["results"][0]["status"] == "COMPLETED"
        assert body["results"][0]["distance_km"] == "10.0"

    def test_custom_range(self, client):
        client_obj = ClientFactory()
        vehicle = _vehicle_with_device(client_obj)
        now = timezone.now()
        old = now - datetime.timedelta(days=5)
        TelemetryEventFactory(
            device=vehicle.tracking_device, vehicle=vehicle, client=client_obj,
            timestamp=old, ignition=True, odometer=50,
        )
        TelemetryEventFactory(
            device=vehicle.tracking_device, vehicle=vehicle, client=client_obj,
            timestamp=old + datetime.timedelta(minutes=15), ignition=False, odometer=60,
        )
        client.force_login(UserFactory(role=_view_role()))

        response = client.get(
            reverse("trip-report-data"),
            {"range": "custom", "from": old.date().isoformat(), "to": old.date().isoformat()},
        )
        body = response.json()
        assert body["range"] == {"key": "custom", "start": old.date().isoformat(), "end": old.date().isoformat()}
        assert body["count"] == 1

    def test_garbage_range_falls_back_to_today_not_a_500(self, client):
        client.force_login(UserFactory(role=_view_role()))
        response = client.get(reverse("trip-report-data"), {"range": "not-a-real-range"})
        assert response.status_code == 200
        assert response.json()["range"]["key"] == "not-a-real-range"  # echoed as given
        assert response.json()["count"] == 0

    def test_vehicle_and_search_filters_are_wired_through(self, client):
        client_obj = ClientFactory()
        a = _vehicle_with_device(client_obj)
        a.registration_number = "KA51EH6363"
        a.save(update_fields=["registration_number"])
        b = _vehicle_with_device(client_obj)
        now = timezone.now()
        _departure(a, client_obj, now - datetime.timedelta(minutes=1))
        _departure(b, client_obj, now - datetime.timedelta(minutes=1))
        client.force_login(UserFactory(role=_view_role()))

        by_vehicle = client.get(reverse("trip-report-data"), {"vehicle": str(a.uuid)})
        assert by_vehicle.json()["count"] == 1
        assert by_vehicle.json()["results"][0]["vehicle_uuid"] == str(a.uuid)

        by_search = client.get(reverse("trip-report-data"), {"q": "6363"})
        assert by_search.json()["count"] == 1

    def test_client_user_is_scoped_to_their_own_fleet(self, client):
        mine, theirs = ClientFactory(), ClientFactory()
        my_vehicle = _vehicle_with_device(mine)
        their_vehicle = _vehicle_with_device(theirs)
        now = timezone.now()
        _departure(my_vehicle, mine, now - datetime.timedelta(minutes=1))
        _departure(their_vehicle, theirs, now - datetime.timedelta(minutes=1))

        client.force_login(UserFactory(role=_view_role(), client=mine))
        response = client.get(reverse("trip-report-data"))
        body = response.json()
        assert body["count"] == 1
        assert body["results"][0]["vehicle_uuid"] == str(my_vehicle.uuid)

    def test_throttle_shares_the_fleet_read_bucket(self):
        from apps.tracking.api_views import TripReportDataView
        from apps.tracking.authentication import FleetReadRateThrottle

        assert TripReportDataView.throttle_classes == [FleetReadRateThrottle]


class TestRouteAPI:
    def _route(self, client, vehicle, start, end=None, **extra):
        params = {"vehicle": str(vehicle.uuid), "start": start.isoformat()}
        if end is not None:
            params["end"] = end.isoformat()
        params.update(extra)
        return client.get(reverse("trip-report-route"), params)

    def test_requires_authentication(self, client):
        assert client.get(reverse("trip-report-route")).status_code in (401, 403)

    def test_requires_permission(self, client):
        client.force_login(UserFactory(role=None))
        assert client.get(reverse("trip-report-route")).status_code == 403

    def test_missing_query_params_is_a_400_not_a_500(self, client):
        client.force_login(UserFactory(role=_view_role()))
        assert client.get(reverse("trip-report-route")).status_code == 400
        assert client.get(reverse("trip-report-route"), {"vehicle": "x"}).status_code == 400
        assert client.get(reverse("trip-report-route"), {"vehicle": "x", "start": "not-a-date"}).status_code == 400

    def test_returns_the_points_recorded_during_the_trip(self, client):
        client_obj = ClientFactory()
        vehicle = _vehicle_with_device(client_obj)
        start = timezone.now().replace(minute=0, second=0, microsecond=0)
        for i in range(5):
            TelemetryEventFactory(
                device=vehicle.tracking_device, vehicle=vehicle, client=client_obj,
                timestamp=start + datetime.timedelta(minutes=i), ignition=True,
                latitude=f"12.97160{i}", longitude=f"77.59460{i}",
            )
        # outside the window — must not be included
        TelemetryEventFactory(
            device=vehicle.tracking_device, vehicle=vehicle, client=client_obj,
            timestamp=start - datetime.timedelta(hours=1), ignition=True,
        )
        client.force_login(UserFactory(role=_view_role()))

        response = self._route(client, vehicle, start, start + datetime.timedelta(minutes=4))
        assert response.status_code == 200
        body = response.json()
        assert body["vehicle_uuid"] == str(vehicle.uuid)
        assert body["truncated"] is False
        assert len(body["points"]) == 5
        assert body["points"][0]["lat"] == "12.971600"
        assert body["points"][-1]["lat"] == "12.971604"

    def test_no_gps_fix_readings_are_excluded_from_the_plotted_route(self, client):
        """Regression: this fleet's real devices report (0, 0) before they
        acquire a fix (seen directly in production: gps_status=0 readings at
        lat=lon=0 right as ignition turns on) — plotting that point drew a
        spurious line out to Null Island and collapsed the real route."""
        client_obj = ClientFactory()
        vehicle = _vehicle_with_device(client_obj)
        start = timezone.now().replace(minute=0, second=0, microsecond=0)
        TelemetryEventFactory(
            device=vehicle.tracking_device, vehicle=vehicle, client=client_obj,
            timestamp=start, ignition=True, latitude="0", longitude="0",
        )
        for i in range(1, 4):
            TelemetryEventFactory(
                device=vehicle.tracking_device, vehicle=vehicle, client=client_obj,
                timestamp=start + datetime.timedelta(minutes=i), ignition=True,
                latitude=f"12.97160{i}", longitude=f"77.59460{i}",
            )
        client.force_login(UserFactory(role=_view_role()))

        response = self._route(client, vehicle, start, start + datetime.timedelta(minutes=3))
        body = response.json()
        assert len(body["points"]) == 3  # the (0, 0) reading dropped, not just left first
        assert all(p["lat"] != "0.000000" for p in body["points"])
        assert body["points"][0]["lat"] == "12.971601"

    def test_omitted_end_reads_up_to_now_for_an_active_trip(self, client):
        client_obj = ClientFactory()
        vehicle = _vehicle_with_device(client_obj)
        start = timezone.now() - datetime.timedelta(minutes=10)
        TelemetryEventFactory(device=vehicle.tracking_device, vehicle=vehicle, client=client_obj, timestamp=start, ignition=True)
        TelemetryEventFactory(
            device=vehicle.tracking_device, vehicle=vehicle, client=client_obj,
            timestamp=timezone.now() - datetime.timedelta(minutes=1), ignition=True,
        )
        client.force_login(UserFactory(role=_view_role()))

        response = self._route(client, vehicle, start)
        assert response.status_code == 200
        assert len(response.json()["points"]) == 2

    def test_large_routes_are_downsampled_keeping_start_and_end(self, client):
        client_obj = ClientFactory()
        vehicle = _vehicle_with_device(client_obj)
        start = timezone.now().replace(minute=0, second=0, microsecond=0)
        for i in range(400):
            TelemetryEventFactory(
                device=vehicle.tracking_device, vehicle=vehicle, client=client_obj,
                timestamp=start + datetime.timedelta(seconds=i), ignition=True,
                latitude="12.971600", longitude=f"{77.0 + i / 1000:.6f}",
            )
        client.force_login(UserFactory(role=_view_role()))

        response = self._route(client, vehicle, start, start + datetime.timedelta(seconds=399))
        body = response.json()
        assert body["truncated"] is True
        assert len(body["points"]) <= 300
        assert body["points"][0]["lon"] == "77.000000"
        assert body["points"][-1]["lon"] == "77.399000"

    def test_unknown_vehicle_is_404(self, client):
        client.force_login(UserFactory(role=_view_role()))
        response = client.get(
            reverse("trip-report-route"),
            {"vehicle": "00000000-0000-0000-0000-000000000000", "start": timezone.now().isoformat()},
        )
        assert response.status_code == 404

    def test_another_clients_vehicle_is_404_not_403(self, client):
        mine, theirs = ClientFactory(), ClientFactory()
        their_vehicle = _vehicle_with_device(theirs)
        client.force_login(UserFactory(role=_view_role(), client=mine))
        response = self._route(client, their_vehicle, timezone.now())
        assert response.status_code == 404

    def test_vehicle_without_a_gps_device_is_404(self, client):
        vehicle = VehicleFactory()
        client.force_login(UserFactory(role=_view_role()))
        response = self._route(client, vehicle, timezone.now())
        assert response.status_code == 404
