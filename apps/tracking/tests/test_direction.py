"""Vehicle direction (apps.tracking.direction -> fleet feed ``direction`` -> Live Tracking marker/popup).

The comms App DB reports heading 0 for every reading, so the direction comes from
the device heading only when the device really reports one, else from the bearing
of the vehicle's own GPS track."""

import datetime
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Permission
from apps.accounts.tests.factories import UserFactory
from apps.core.tests.factories import role_with
from apps.tracking.direction import bearing, compass, direction_for, distance_m
from apps.tracking.models import TelemetryEvent, VehicleCurrentTelemetry
from apps.tracking.tests.factories import TrackingDeviceFactory
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db
S = datetime.timedelta(seconds=30)
LAT, LON = 12.98, 77.53
DEG_PER_100M = 100 / 111320  # latitude degrees per 100 m


@pytest.mark.parametrize("deg, code, label", [
    (0, "N", "North"), (22, "N", "North"), (23, "NE", "North-East"), (45, "NE", "North-East"), (90, "E", "East"),
    (135, "SE", "South-East"), (180, "S", "South"), (225, "SW", "South-West"), (270, "W", "West"),
    (315, "NW", "North-West"), (338, "N", "North"), (359, "N", "North"),
])
def test_compass_points(deg, code, label):
    assert compass(deg) == (code, label)


def test_bearing_and_distance():
    origin = (LAT, LON)
    assert round(bearing(origin, (LAT + 0.01, LON))) == 0  # north
    assert round(bearing(origin, (LAT, LON + 0.01))) == 90  # east
    assert round(bearing(origin, (LAT - 0.01, LON))) == 180  # south
    assert round(bearing(origin, (LAT, LON - 0.01))) == 270  # west
    assert 99 < distance_m(origin, (LAT + DEG_PER_100M, LON)) < 101


class _Row:
    def __init__(self, lat, lon, heading=0):
        self.latitude, self.longitude, self.heading, self.timestamp = lat, lon, heading, timezone.now()


def _hist(*points):  # newest first; heading 0 like the comms App DB
    now = timezone.now()
    return [(now - S * (i + 1), lat, lon, 0) for i, (lat, lon) in enumerate(points)]


def test_moving_direction_comes_from_the_gps_track_when_the_device_reports_0():
    # came from 200 m south-west -> heading north-east
    d = direction_for(_Row(LAT, LON), _hist((LAT - 0.0013, LON - 0.0013)), moving=True)
    assert (d["cardinal"], d["label"], d["source"], d["moving"]) == ("NE", "North-East", "gps", True)
    assert 40 <= d["degrees"] <= 50


def test_gps_jitter_is_not_movement():
    jitter = _hist((LAT + 0.00005, LON), (LAT, LON + 0.00008), (LAT - 0.00006, LON))  # < 25 m
    d = direction_for(_Row(LAT, LON), jitter, moving=False)
    assert d["degrees"] is None and d["moving"] is False


def test_skips_points_too_close_and_uses_the_last_real_move():
    hist = _hist((LAT + 0.00005, LON), (LAT, LON - 2 * DEG_PER_100M))  # jitter, then 200 m west of here
    d = direction_for(_Row(LAT, LON), hist, moving=True)
    assert d["cardinal"] == "E"


def test_stationary_vehicle_keeps_its_last_heading():
    d = direction_for(_Row(LAT, LON), _hist((LAT + 0.002, LON)), moving=False)  # arrived from the north
    assert d["cardinal"] == "S" and d["moving"] is False


def test_real_device_heading_wins_while_moving():
    hist = [(timezone.now() - S, LAT - 0.002, LON, 90)]  # the device does report headings
    d = direction_for(_Row(LAT, LON, heading=270), hist, moving=True)
    assert (d["degrees"], d["source"]) == (270, "device")


def test_no_fix_and_no_history_is_unknown():
    assert direction_for(_Row(0, 0), [], moving=True)["degrees"] is None
    assert direction_for(_Row(LAT, LON), [], moving=True)["degrees"] is None


def test_fleet_feed_carries_the_direction(client):
    vehicle = VehicleFactory()
    device = TrackingDeviceFactory(vehicle=vehicle, imei="350424067315497")
    now = timezone.now()
    for i, lat in enumerate([LAT - 4 * DEG_PER_100M, LAT - 2 * DEG_PER_100M]):  # driving north
        TelemetryEvent.objects.create(device=device, vehicle=vehicle, timestamp=now - S * (2 - i),
                                      latitude=round(lat, 6), longitude=LON, speed=40, heading=0)
    TelemetryEvent.objects.create(device=device, vehicle=vehicle, timestamp=now, latitude=LAT, longitude=LON, speed=40, heading=0)
    VehicleCurrentTelemetry.objects.create(vehicle=vehicle, device=device, timestamp=now, latitude=LAT, longitude=LON,
                                           speed=40, heading=0)
    client.force_login(UserFactory(role=role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW))))
    [item] = client.get(reverse("fleet-telemetry-current")).json()["results"]
    assert item["direction"]["cardinal"] == "N" and item["direction"]["moving"] is True
    assert item["direction"]["source"] == "gps"
    assert item["heading"] == 0  # the existing raw field is untouched


def test_offline_vehicle_is_not_shown_as_moving(client):
    vehicle = VehicleFactory()
    device = TrackingDeviceFactory(vehicle=vehicle, imei="350424067315498")
    old = timezone.now() - datetime.timedelta(days=2)
    TelemetryEvent.objects.create(device=device, vehicle=vehicle, timestamp=old - S, latitude=LAT - 0.002, longitude=LON, speed=40)
    VehicleCurrentTelemetry.objects.create(vehicle=vehicle, device=device, timestamp=old, latitude=LAT, longitude=LON, speed=40)
    client.force_login(UserFactory(role=role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW))))
    [item] = client.get(reverse("fleet-telemetry-current")).json()["results"]
    assert item["direction"]["cardinal"] == "N" and item["direction"]["moving"] is False  # last heading kept


def test_marker_and_popup_use_the_direction():
    base = Path(settings.BASE_DIR) / "static"
    icons = (base / "js" / "vehicle_icons.js").read_text(encoding="utf-8")
    live = (base / "js" / "live_tracking.js").read_text(encoding="utf-8")
    assert "const heading = direction.degrees !== null ? direction.degrees : validHeading(vehicle.heading);" in icons
    assert 'class="vm-dir"' in icons and "has-dir" in icons
    assert 'row("Direction", directionHtml(vehicle))' in live
