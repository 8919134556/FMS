"""Dashboard Live Fleet / Today's Trips / Alerts (apps.core.dashboard_services + _live_fleet.html).

All numbers come from the same services as the Live Tracking page, Trip Report and
Alert Report — these tests pin the classification, the scoping and the permission gates."""

import datetime
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Permission
from apps.accounts.tests.factories import UserFactory
from apps.alerts.models import Alert
from apps.alerts.tests.factories import AlertFactory
from apps.clients.tests.factories import ClientFactory
from apps.core.dashboard_services import alerts_overview, live_fleet_summary, live_status_key, todays_trip_activity
from apps.core.tests.factories import role_with
from apps.tracking.models import VehicleCurrentTelemetry
from apps.tracking.tests.factories import TrackingDeviceFactory
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db

TRACKING = (Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW)
ALERTS = (Permission.Module.ALERT, Permission.Action.VIEW)
_imei = iter(range(350424067300000, 350424067399999))


def _tracked(*, speed=0, ignition=True, age=datetime.timedelta(seconds=10), immobilizer=None, client=None):
    vehicle = VehicleFactory(**({"client": client} if client else {}))
    device = TrackingDeviceFactory(vehicle=vehicle, imei=str(next(_imei)))
    VehicleCurrentTelemetry.objects.create(
        vehicle=vehicle, device=device, timestamp=timezone.now() - age, latitude=12.98, longitude=77.53,
        speed=speed, ignition=ignition, immobilizer=immobilizer,
    )
    return vehicle


@pytest.mark.parametrize("vehicle, key", [
    ({"connection_status": "OFFLINE", "movement_state": "MOVING", "ignition": True}, "offline"),
    ({"connection_status": "ONLINE", "movement_state": "MOVING", "ignition": True}, "moving"),
    ({"connection_status": "ONLINE", "movement_state": "IDLE", "ignition": False}, "stopped"),
    ({"connection_status": "STALE", "movement_state": "IDLE", "ignition": True}, "idle"),
    ({"connection_status": "ONLINE", "movement_state": None, "ignition": None}, "idle"),
])
def test_status_rule_matches_the_live_map(vehicle, key):
    assert live_status_key(vehicle) == key  # == static/js/vehicle_icons.js statusFor


def test_live_fleet_counts_from_the_fleet_feed():
    _tracked(speed=40)  # moving
    _tracked(speed=0, ignition=True)  # idle
    _tracked(speed=0, ignition=False, immobilizer=True)  # ignition off, immobilized
    _tracked(speed=40, age=datetime.timedelta(days=2))  # offline
    VehicleFactory()  # no telemetry: not tracked
    summary = live_fleet_summary(UserFactory(role=role_with(TRACKING)))
    assert (summary["tracked"], summary["moving"], summary["idle"], summary["stopped"], summary["offline"]) == (4, 1, 1, 1, 1)
    assert summary["immobilized"] == 1 and summary["online"] == 3
    assert summary["share"] == {"moving": 25, "idle": 25, "stopped": 25, "offline": 25}


def test_live_fleet_is_scoped_and_permission_gated():
    mine, theirs = ClientFactory(), ClientFactory()
    _tracked(speed=40, client=mine)
    _tracked(speed=40, client=theirs)
    assert live_fleet_summary(UserFactory(role=role_with(TRACKING), client=mine))["tracked"] == 1
    assert live_fleet_summary(UserFactory(role=role_with(ALERTS))) is None
    assert todays_trip_activity(UserFactory(role=role_with(ALERTS))) is None


def test_todays_trips_use_the_trip_report_summary():
    _tracked(speed=0)
    activity = todays_trip_activity(UserFactory(role=role_with(TRACKING)))
    assert (activity["total_trips"], activity["active_trips"], activity["avg_duration_text"]) == (0, 0, "—")
    assert activity["equipped_vehicles"] == 1 and activity["vehicles_with_trips"] == 0
    assert activity["utilization_pct"] == 0 and activity["avg_moving_speed_kmh"] is None
    assert activity["recent_trips"] == []


def test_average_moving_speed_counts_only_moving_readings_today():
    from apps.tracking.models import TelemetryEvent

    vehicle = _tracked(speed=40)
    device = vehicle.tracking_device
    now = timezone.now()
    for i, speed in enumerate([30, 50, 0, 2]):  # 0 and 2 km/h are below the movement threshold
        TelemetryEvent.objects.create(device=device, vehicle=vehicle, timestamp=now - datetime.timedelta(minutes=i + 1),
                                      latitude=12.98, longitude=77.53, speed=speed)
    assert todays_trip_activity(UserFactory(role=role_with(TRACKING)))["avg_moving_speed_kmh"] == 40


def test_fleet_activity_counts_vehicles_per_hour():
    from apps.core.dashboard_services import fleet_activity_24h
    from apps.tracking.models import TelemetryEvent

    a, b = _tracked(speed=40), _tracked(speed=0)
    now = timezone.now()
    for vehicle, speed in ((a, 40), (a, 45), (b, 0)):  # a: 2 moving readings this hour, b: 1 stationary
        TelemetryEvent.objects.create(device=vehicle.tracking_device, vehicle=vehicle, timestamp=now - datetime.timedelta(seconds=30 * (speed + 1)),
                                      latitude=12.98, longitude=77.53, speed=speed)
    TelemetryEvent.objects.create(device=a.tracking_device, vehicle=a, timestamp=now - datetime.timedelta(hours=30),
                                  latitude=12.98, longitude=77.53, speed=50)  # older than 24 h: not counted
    activity = fleet_activity_24h(UserFactory(role=role_with(TRACKING)))
    assert len(activity["labels"]) == 24 and activity["has_data"] is True
    assert sum(activity["reporting"]) >= 2 and activity["reporting"][-1] + activity["reporting"][-2] >= 2
    assert max(activity["moving"]) == 1  # one distinct moving vehicle, not two readings
    assert activity["alerts"] is None  # no alert permission -> no alert series
    assert fleet_activity_24h(UserFactory(role=role_with(ALERTS))) is None


def test_recent_activity_merges_alerts_and_trips_newest_first():
    from apps.core.dashboard_services import recent_activity_feed

    now = timezone.now()
    alerts = {"recent": [{"uuid": "u1", "type": "Panic", "vehicle": "KA01", "level": "CRITICAL", "level_label": "Critical",
                          "occurred_at": now - datetime.timedelta(minutes=5), "active": True, "status": "Open"}]}
    trips = {"recent_trips": [{"vehicle": "KA02", "vehicle_uuid": "x", "status": "COMPLETED", "start_at": now - datetime.timedelta(hours=1),
                               "end_at": now - datetime.timedelta(minutes=1), "distance_km": 12, "start_location": ""}]}
    feed = recent_activity_feed(alerts, trips)
    assert [(i["kind"], i["title"]) for i in feed] == [("trip", "Trip completed"), ("alert", "Panic")]
    assert recent_activity_feed(None, None) == []


def _alert(vehicle, *, severity="HIGH", status=Alert.Status.OPEN, days_ago=0, category=Alert.Category.OVER_SPEEDING,
           cleared=True, client=None, archived=False):
    when = timezone.now() - datetime.timedelta(days=days_ago)
    return AlertFactory(vehicle=vehicle, client=client, category=category, severity=severity, status=status,
                        occurred_at=when, is_archived=archived, signal_cleared_at=when if cleared else None)


def test_alerts_overview_counts_trend_and_latest():
    vehicle = _tracked()
    _alert(vehicle, severity="CRITICAL", category=Alert.Category.PANIC, cleared=False)  # open, active now
    _alert(vehicle, severity="HIGH", days_ago=1)
    _alert(vehicle, severity="MEDIUM", status=Alert.Status.RESOLVED, days_ago=2)
    _alert(vehicle, severity="HIGH", days_ago=10)  # outside the 7-day chart, still open
    AlertFactory(category=Alert.Category.TRIP_DELAYED, severity="HIGH")  # not a vehicle alert
    info = alerts_overview(UserFactory(role=role_with(ALERTS)))
    assert info["open_total"] == 3
    assert dict(info["open_by_level"]) == {"CRITICAL": 1, "HIGH": 2, "MEDIUM": 0, "LOW": 0}
    assert info["active_now"] == 1 and info["week_total"] == 3
    assert len(info["trend"]["labels"]) == 7
    assert info["trend"]["series"]["CRITICAL"][-1] == 1 and info["trend"]["series"]["HIGH"][-2] == 1
    assert [a["level"] for a in info["recent"]][:1] == ["CRITICAL"] and info["recent"][0]["active"] is True


def test_alerts_overview_is_scoped_and_permission_gated():
    mine, theirs = ClientFactory(), ClientFactory()
    _alert(_tracked(client=mine), client=mine)
    _alert(_tracked(client=theirs), client=theirs)
    assert alerts_overview(UserFactory(role=role_with(ALERTS), client=mine))["open_total"] == 1
    assert alerts_overview(UserFactory(role=role_with(TRACKING))) is None


def test_dashboard_shows_the_live_section_only_with_permission(client):
    _tracked(speed=40)
    client.force_login(UserFactory(role=role_with(TRACKING, ALERTS)))
    html = client.get(reverse("core:dashboard")).content.decode()
    assert 'id="dashboardLiveFleet"' in html and 'data-live-count="moving"' in html
    assert 'id="liveStatusChart"' in html and "Open Alerts" in html
    assert "js/dashboard_live.js" in html and "js/vehicle_icons.js" in html

    client.force_login(UserFactory(role=role_with((Permission.Module.VEHICLE, Permission.Action.VIEW))))
    html = client.get(reverse("core:dashboard")).content.decode()
    assert 'id="dashboardLiveFleet"' not in html and "Open Alerts" not in html


def test_chart_script_has_no_stray_parenthesis():
    """A '})' closing the block-bodied Fleet Status builder made the whole chart script a
    SyntaxError, so no dashboard chart was drawn."""
    js = (Path(settings.BASE_DIR) / "templates" / "dashboard" / "partials" / "_charts_js.html").read_text(encoding="utf-8")
    fleet = js[js.index("fleetLifecycleChart:"):js.index("activityTrendChart:")]
    assert fleet.rstrip().endswith("},") and not fleet.rstrip().endswith("}),")
    assert 'token("--fms-surface") || token("--fms-card")' in js  # --fms-surface is not a defined token


def test_gps_trips_week_shape_and_permission():
    from apps.core.dashboard_services import gps_trips_week

    _tracked(speed=0)
    week = gps_trips_week(UserFactory(role=role_with(TRACKING)))
    assert len(week["trend"]["labels"]) == 7 and len(week["trend"]["trips"]) == 7 and len(week["trend"]["distance_km"]) == 7
    assert week["total"] == 0 and week["active"] == [] and week["recent"] == []
    assert gps_trips_week(UserFactory(role=role_with(ALERTS))) is None


def test_trip_widgets_use_gps_trips_with_tracking_access(client):
    """With GPS access the trip widgets show detected trips; the dispatch-only widgets
    (Today's Operations, trips by status) stay hidden while there are no dispatch trips."""
    _tracked(speed=0)
    trip_view = (Permission.Module.TRIP, Permission.Action.VIEW)
    client.force_login(UserFactory(role=role_with(TRACKING, trip_view)))
    html = client.get(reverse("core:dashboard")).content.decode()
    assert "GPS-detected trips and distance per day" in html
    assert "No vehicle is on a trip right now" in html  # Today's Active Trips (GPS)
    assert "Trips scheduled to start" not in html  # dispatch statuses hidden without dispatch trips

    client.force_login(UserFactory(role=role_with(trip_view)))  # dispatch-only user: unchanged widgets
    html = client.get(reverse("core:dashboard")).content.decode()
    assert "GPS-detected trips" not in html and "Scheduled vs. completed, per day" in html
