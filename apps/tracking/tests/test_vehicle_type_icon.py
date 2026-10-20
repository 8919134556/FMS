"""Live Tracking vehicle-type icons: where the type comes from.

comms current_table.vechile_type -> VehicleCurrentTelemetry.vehicle_type ->
fleet feed ``vehicle_type`` (else the FMS Vehicle Type) -> FmsVehicleIcons
(static/js/vehicle_icons.js) picks the icon in the browser."""

import datetime
from pathlib import Path

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Permission
from apps.alerts.models import Alert
from apps.alerts.tests.factories import AlertFactory
from apps.accounts.tests.factories import UserFactory
from apps.core.tests.factories import role_with
from apps.tracking import comms_sync
from apps.tracking.models import VehicleCurrentTelemetry
from apps.tracking.tests.factories import TrackingDeviceFactory
from apps.vehicles.models import VehicleType
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db

IMEI = "350424067315497"
S = datetime.timedelta(seconds=1)


def _device(vehicle_type_name=None):
    kwargs = {}
    if vehicle_type_name:
        kwargs["vehicle_type"] = VehicleType.objects.create(code=vehicle_type_name.upper()[:20], name=vehicle_type_name)
    vehicle = VehicleFactory(**kwargs)
    TrackingDeviceFactory(vehicle=vehicle, imei=IMEI)
    return vehicle


def _row(when, i=1, **extra):
    row = {"id": i, "unitno": IMEI, "tracktime": when, "lat": 12.98 + i * 1e-5, "lon": 77.53, "speed": 0.0,
           "direction": "0", "ignition": 1, "odometer": 1000, "gpsodometer": 0, "gpsstatus": 1, "location": "Bengaluru"}
    row.update(extra)
    return row


def _now_local():
    return timezone.now().astimezone(comms_sync._local_timezone()).replace(tzinfo=None, microsecond=0)


def _feed(client):
    client.force_login(UserFactory(role=role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW))))
    [item] = client.get("/api/v1/tracking/fleet/current/").json()["results"]
    return item


def test_comms_current_row_sets_the_vehicle_type_as_stored():
    vehicle = _device()
    comms_sync.apply_rows([_row(_now_local(), vechile_type="bike")], comms_sync.SyncReport())
    assert VehicleCurrentTelemetry.objects.get(vehicle=vehicle).vehicle_type == "bike"  # not modified/normalised


def test_history_rows_without_a_type_never_wipe_it():
    vehicle = _device()
    t = _now_local() - datetime.timedelta(minutes=5)
    comms_sync.apply_rows([_row(t, vechile_type="Bus")], comms_sync.SyncReport())
    comms_sync.apply_rows([_row(t + 30 * S, i=2), _row(t + 60 * S, i=3)], comms_sync.SyncReport())  # history rows
    current = VehicleCurrentTelemetry.objects.get(vehicle=vehicle)
    assert current.timestamp == (t + 60 * S).replace(tzinfo=comms_sync._local_timezone())  # position moved on
    assert current.vehicle_type == "Bus"  # ...the type stayed


def test_a_type_change_in_comms_is_followed_even_for_the_same_reading():
    vehicle = _device()
    t = _now_local()
    comms_sync.apply_rows([_row(t, vechile_type="Bike")], comms_sync.SyncReport())
    comms_sync.apply_rows([_row(t, vechile_type="Truck")], comms_sync.SyncReport())  # current_table re-read, same time
    assert VehicleCurrentTelemetry.objects.get(vehicle=vehicle).vehicle_type == "Truck"


def test_feed_uses_the_comms_type_else_the_fms_vehicle_type(client):
    vehicle = _device("Van")
    comms_sync.apply_rows([_row(_now_local())], comms_sync.SyncReport())  # comms has no type for it
    assert _feed(client)["vehicle_type"] == "Van"
    comms_sync.apply_rows([_row(_now_local() + 30 * S, i=2, vechile_type="bike")], comms_sync.SyncReport())
    assert _feed(client)["vehicle_type"] == "bike"
    assert VehicleCurrentTelemetry.objects.get(vehicle=vehicle).vehicle.vehicle_type.name == "Van"  # master untouched


def test_blank_comms_type_falls_back_to_the_fms_type(client):
    vehicle = _device("Bus")
    comms_sync.apply_rows([_row(_now_local(), vechile_type="   ")], comms_sync.SyncReport())
    assert VehicleCurrentTelemetry.objects.get(vehicle=vehicle).vehicle_type == ""
    assert _feed(client)["vehicle_type"] == "Bus"


def test_unrecognised_type_is_passed_through_for_the_general_icon(client):
    """Every FMS vehicle has a type (required); an unrecognised one like the real "Type 0" with no comms
    type is sent as-is and the browser's FmsVehicleIcons maps it to the general vehicle icon."""
    _device("Type 0")
    comms_sync.apply_rows([_row(_now_local())], comms_sync.SyncReport())
    assert _feed(client)["vehicle_type"] == "Type 0"


def test_the_bridge_reads_the_type_from_current_table_only():
    assert "vechile_type" in comms_sync._CURRENT_COLUMNS
    assert "vechile_type" not in comms_sync._COLUMNS  # history_table has no such column


def test_live_page_loads_the_shared_icon_mapping(client):
    client.force_login(UserFactory(role=role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW))))
    html = client.get(reverse("tracking:live_map")).content.decode()
    assert html.index("js/vehicle_icons.js") < html.index("js/live_tracking.js")
    assert 'id="liveVehicleModalIcon"' in html


def test_marker_is_one_pin_and_one_vehicle_graphic():
    """No status dot on the pin; the vehicle picture never rotates — only the direction arrow does."""
    from django.conf import settings

    js = (Path(settings.BASE_DIR) / "static" / "js" / "vehicle_icons.js").read_text(encoding="utf-8")
    marker = js[js.index("function markerHtml"):js.index("function markerIcon")]
    assert 'class="vm-art"' in marker and 'class="vm-pin"' in js
    assert "vm-status" not in marker and "vm-heading" not in marker
    assert 'querySelector(".vm-art").style.transform' not in js  # the picture stays upright
    assert 'arrow.style.transform = `rotate(' in js
    assert "badge-dot" not in js
    assert "--vm-type" not in js  # colour means status only; the illustration never takes a status colour


def test_pin_colour_follows_status_with_alert_first():
    from django.conf import settings

    css = (Path(settings.BASE_DIR) / "static" / "css" / "live_tracking.css").read_text(encoding="utf-8")
    assert ".vm-pin-shape { fill: var(--vm-pin);" in css
    assert ".vm--moving { --vm-pin: var(--fms-success); }" in css
    assert ".vm--idle { --vm-pin: var(--fms-warning); }" in css
    assert ".vm--stopped, .vm--offline { --vm-pin: var(--fms-text-muted); }" in css
    assert ".vm.vm--alert { --vm-pin: var(--fms-danger); }" in css  # higher specificity: wins over all


# --- active_alert in the fleet feed: what turns the pin red -------------------------------------------

def _alert(vehicle, category, severity="HIGH", **extra):
    return AlertFactory(vehicle=vehicle, category=category, severity=severity,
                        occurred_at=timezone.now() - datetime.timedelta(minutes=1), **extra)


def test_feed_has_no_active_alert_without_one(client):
    _device()
    comms_sync.apply_rows([_row(_now_local())], comms_sync.SyncReport())
    assert _feed(client)["active_alert"] is None


def test_an_alert_still_in_progress_is_reported_and_clears_with_its_condition(client):
    vehicle = _device()
    comms_sync.apply_rows([_row(_now_local())], comms_sync.SyncReport())
    alert = _alert(vehicle, Alert.Category.OVER_SPEEDING)
    item = _feed(client)["active_alert"]
    assert item["category"] == "OVER_SPEEDING" and item["label"] == "Over Speeding" and item["severity"] == "HIGH"
    alert.signal_cleared_at = timezone.now()
    alert.save()
    assert _feed(client)["active_alert"] is None  # condition over -> back to the movement colour


def test_most_severe_active_alert_wins(client):
    vehicle = _device()
    comms_sync.apply_rows([_row(_now_local())], comms_sync.SyncReport())
    _alert(vehicle, Alert.Category.LOW_VOLTAGE, "MEDIUM")
    _alert(vehicle, Alert.Category.PANIC, "CRITICAL")
    assert _feed(client)["active_alert"]["category"] == "PANIC"


def test_resolved_archived_idle_and_geofence_presence_alerts_do_not_turn_the_pin_red(client):
    vehicle = _device()
    comms_sync.apply_rows([_row(_now_local())], comms_sync.SyncReport())
    _alert(vehicle, Alert.Category.PANIC, status=Alert.Status.RESOLVED)
    _alert(vehicle, Alert.Category.MAIN_POWER_DISCONNECTED, is_archived=True)
    _alert(vehicle, Alert.Category.IDLE, "MEDIUM")  # idle is the amber pin already
    _alert(vehicle, Alert.Category.GEOFENCE_ENTRY, "MEDIUM")  # stays open while inside the zone
    _alert(vehicle, Alert.Category.GEOFENCE_EXIT, "MEDIUM")
    assert _feed(client)["active_alert"] is None
    _alert(vehicle, Alert.Category.GEOFENCE_SPEEDING)
    assert _feed(client)["active_alert"]["category"] == "GEOFENCE_SPEEDING"


def test_acknowledged_alert_stays_red_while_the_condition_lasts(client):
    vehicle = _device()
    comms_sync.apply_rows([_row(_now_local())], comms_sync.SyncReport())
    _alert(vehicle, Alert.Category.PANIC, "CRITICAL", status=Alert.Status.ACKNOWLEDGED)
    assert _feed(client)["active_alert"]["category"] == "PANIC"
