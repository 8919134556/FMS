"""Live Map popup = the vehicle's CURRENT position only (no route/history);
a (0,0) "no GPS fix" reading never replaces the last good current position;
the trip popup gets a full-detail route (existing endpoint, optional
``max_points``) and animates a vehicle marker along it."""

import datetime
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Permission
from apps.accounts.tests.factories import UserFactory
from apps.core.tests.factories import role_with
from apps.tracking import services, trip_report
from apps.tracking.models import VehicleCurrentTelemetry
from apps.tracking.providers.base import NormalizedEvent
from apps.tracking.tests.factories import TelemetryEventFactory, TrackingDeviceFactory, VehicleCurrentTelemetryFactory
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db

STATIC_JS = Path(settings.BASE_DIR) / "static" / "js"


class TestCurrentPositionKeepsTheLastGoodFix:
    def _setup(self):
        vehicle = VehicleFactory()
        device = TrackingDeviceFactory(vehicle=vehicle)
        now = timezone.now()
        VehicleCurrentTelemetryFactory(vehicle=vehicle, device=device, timestamp=now, latitude="12.985443",
                                       longitude="77.539423", location="Basaveshwaranagar", speed="10.00")
        return vehicle, device, now

    def test_newer_no_fix_reading_keeps_position_but_advances_the_rest(self):
        vehicle, device, now = self._setup()
        no_fix = NormalizedEvent(timestamp=now + datetime.timedelta(seconds=30), latitude=0, longitude=0,
                                 speed=0, ignition=True, odometer=120, metadata={"location": ""})
        services.upsert_current_telemetry(vehicle=vehicle, device=device, event=no_fix)
        current = VehicleCurrentTelemetry.objects.get(vehicle=vehicle)
        assert (float(current.latitude), float(current.longitude)) == (12.985443, 77.539423)
        assert current.location == "Basaveshwaranagar"
        assert current.timestamp == no_fix.timestamp  # "last update" stays honest
        assert current.speed == 0 and current.ignition is True and current.odometer == 120

    def test_a_newer_real_fix_still_moves_the_position(self):
        vehicle, device, now = self._setup()
        moved = NormalizedEvent(timestamp=now + datetime.timedelta(seconds=30), latitude=12.99, longitude=77.54, speed=20)
        services.upsert_current_telemetry(vehicle=vehicle, device=device, event=moved)
        current = VehicleCurrentTelemetry.objects.get(vehicle=vehicle)
        assert (float(current.latitude), float(current.longitude)) == (12.99, 77.54)

    def test_a_vehicle_whose_first_reading_has_no_fix_stores_it_honestly(self):
        vehicle = VehicleFactory()
        device = TrackingDeviceFactory(vehicle=vehicle)
        services.upsert_current_telemetry(vehicle=vehicle, device=device,
                                          event=NormalizedEvent(timestamp=timezone.now(), latitude=0, longitude=0))
        current = VehicleCurrentTelemetry.objects.get(vehicle=vehicle)
        assert (float(current.latitude), float(current.longitude)) == (0, 0)  # nothing better exists yet


class TestRouteDetail:
    def _trip(self, n):
        vehicle = VehicleFactory()
        TrackingDeviceFactory(vehicle=vehicle)
        start = timezone.now() - datetime.timedelta(hours=3)
        for i in range(n):
            TelemetryEventFactory(device=vehicle.tracking_device, vehicle=vehicle, ignition=True,
                                  timestamp=start + datetime.timedelta(seconds=10 * i),
                                  latitude=f"{12.9 + 0.0001 * i:.6f}", longitude="77.500000")
        return vehicle, start, start + datetime.timedelta(seconds=10 * (n - 1))

    def _get(self, client, vehicle, start, end, **extra):
        client.force_login(UserFactory(role=role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW))))
        return client.get(reverse("trip-report-route"),
                          {"vehicle": str(vehicle.uuid), "start": start.isoformat(), "end": end.isoformat(), **extra}).json()

    def test_thumbnails_keep_the_300_point_default(self, client):
        vehicle, start, end = self._trip(400)
        body = self._get(client, vehicle, start, end)
        assert len(body["points"]) == trip_report.MAX_ROUTE_POINTS and body["truncated"] is True

    def test_the_popup_gets_every_recorded_fix_in_order(self, client):
        vehicle, start, end = self._trip(400)
        body = self._get(client, vehicle, start, end, max_points=trip_report.MAX_DETAIL_ROUTE_POINTS)
        assert len(body["points"]) == 400 and body["truncated"] is False
        times = [p["t"] for p in body["points"]]
        assert times == sorted(times)

    def test_max_points_is_bounded_and_tolerates_garbage(self, client):
        vehicle, start, end = self._trip(30)
        assert len(self._get(client, vehicle, start, end, max_points=999999)["points"]) == 30  # capped, all fit
        assert len(self._get(client, vehicle, start, end, max_points=1)["points"]) == 2  # first + last at minimum
        assert len(self._get(client, vehicle, start, end, max_points="lots")["points"]) == 30  # default 300


class TestFrontEndContracts:
    def test_live_map_popup_draws_no_route(self):
        js = (STATIC_JS / "live_tracking.js").read_text(encoding="utf-8")
        assert "L.polyline" not in js and "ROUTE_URL" not in js and "trip-report/route" not in js
        assert "function destroyLiveMap()" in js and "buildLiveMap();" in js
        assert "hasGpsFix(vehicle)" in js  # (0,0) never becomes a marker

    def test_trip_popup_loads_and_tears_down_the_playback(self, client):
        client.force_login(UserFactory(role=role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW))))
        html = client.get(reverse("tracking:trip_report")).content.decode()
        assert html.index("js/route_playback.js") < html.index("js/trip_report.js")
        for element in ("tripPlaybackPlay", "tripPlaybackRestart", "tripPlaybackScrub", "tripPlaybackSpeed"):
            assert f'id="{element}"' in html
        trip_js = (STATIC_JS / "trip_report.js").read_text(encoding="utf-8")
        assert "routePlayer.destroy()" in trip_js and "max_points" in trip_js
        playback = (STATIC_JS / "route_playback.js").read_text(encoding="utf-8")
        assert "cancelAnimationFrame" in playback and "abort.abort()" in playback
