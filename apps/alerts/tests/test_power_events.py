"""MAIN POWER DISCONNECTED alert events: comms ``mainpower`` (V) -> exactly
0 V is power_cut 1 -> one HIGH alert per disconnection, on the same LEVEL
engine as Panic (apps.alerts.events); plus the Live Tracking Main Power column,
the Alert Report / exports / bell, and independence from the other alerts."""

import datetime
import io
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.models import Permission
from apps.accounts.tests.factories import UserFactory
from apps.alerts.events import main_power_flags, main_power_state, power_cut_from_mainpower
from apps.alerts.models import Alert
from apps.clients.tests.factories import ClientFactory
from apps.core.tests.factories import role_with
from apps.notifications.models import Notification
from apps.tracking import comms_sync
from apps.tracking.models import TelemetryEvent, VehicleCurrentTelemetry
from apps.tracking.providers.base import NormalizedEvent
from apps.tracking.services import TelemetryIngestionService
from apps.tracking.tests.factories import TrackingDeviceFactory
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db

S = datetime.timedelta(seconds=1)
IMEI = "350424067315497"


def _base():
    return timezone.now() - datetime.timedelta(minutes=30)


def _vehicle(**kwargs):
    vehicle = VehicleFactory(client=kwargs.pop("client", None) or ClientFactory(), **kwargs)
    TrackingDeviceFactory(vehicle=vehicle)
    return vehicle


def _r(when, mainpower, i=0, **metadata):
    meta = {"location": "Basaveshwaranagar, Bengaluru", **metadata}
    if mainpower is not None:
        meta["mainpower"] = mainpower
        meta.update(main_power_flags(mainpower))
    return NormalizedEvent(timestamp=when, latitude=Decimal(f"{12.98 + i * 0.00001:.6f}"), longitude=Decimal("77.53"),
                           speed=Decimal("0"), ignition=True, metadata=meta)


def _series(vehicle, start, volts, step=30 * S, batch=3):
    readings = [_r(start + k * step, v, i=k) for k, v in enumerate(volts)]
    for k in range(0, len(readings), batch):
        TelemetryIngestionService.ingest_events(device=vehicle.tracking_device, events=readings[k:k + batch])
    return readings


def _alerts(vehicle, category=Alert.Category.MAIN_POWER_DISCONNECTED):
    return list(Alert.objects.filter(category=category, vehicle=vehicle).order_by("occurred_at"))


def _lows(vehicle):
    return _alerts(vehicle, Alert.Category.LOW_VOLTAGE)


@pytest.fixture
def watcher():
    return UserFactory(role=role_with((Permission.Module.ALERT, Permission.Action.VIEW)))


class TestMapping:
    @pytest.mark.parametrize("value, expected", [
        (0, "DISCONNECTED"), (0.0, "DISCONNECTED"), (0.047, "DISCONNECTED"), (4.99, "DISCONNECTED"),
        ("4.999", "DISCONNECTED"), (5, "LOW"), (5.00, "LOW"), (6.491, "LOW"), (8.49, "LOW"), ("8.499", "LOW"),
        (8.5, "NORMAL"), (8.50, "NORMAL"), (10, "NORMAL"), (12, "NORMAL"), (24.2, "NORMAL"),
        (None, None), ("", None), ("x", None), (-1, None), (-0.01, None), (float("nan"), None),
    ])
    def test_exact_voltage_ranges(self, value, expected):
        assert main_power_state(value) == expected

    def test_flags_follow_the_state(self):
        assert main_power_flags(3) == {"power_cut": 1, "low_voltage": 0}
        assert main_power_flags(7) == {"power_cut": 0, "low_voltage": 1}
        assert main_power_flags(12) == {"power_cut": 0, "low_voltage": 0}
        assert main_power_flags(None) == {} and power_cut_from_mainpower(None) is None

    def test_the_bridge_keeps_the_voltage_and_the_flag(self):
        tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        row = {"id": 1, "unitno": IMEI, "tracktime": datetime.datetime(2026, 10, 4, 15, 9, 53), "lat": 12.98,
               "lon": 77.53, "speed": 0.0, "direction": "0", "ignition": 1, "odometer": 1000, "gpsodometer": 0,
               "gpsstatus": 1, "location": "Bengaluru"}
        off = comms_sync.row_to_event({**row, "mainpower": 0.0}, tz)
        on = comms_sync.row_to_event({**row, "mainpower": 12.676}, tz)
        unknown = comms_sync.row_to_event({**row, "mainpower": None}, tz)
        low = comms_sync.row_to_event({**row, "mainpower": 6.491}, tz)
        assert (off.metadata["mainpower"], off.metadata["power_cut"], off.external_power) == (0.0, 1, False)
        assert (on.metadata["mainpower"], on.metadata["power_cut"], on.external_power) == (12.676, 0, True)
        assert (low.metadata["power_cut"], low.metadata["low_voltage"], low.external_power) == (0, 1, True)
        assert "power_cut" not in unknown.metadata and unknown.external_power is None


class TestUserScenario:
    def test_the_documented_sequence(self, watcher):
        """12.5, 12.4, 0 (alert), 0, 0, 0, 12.3 (restored), 12.2, 0 (new alert)."""
        v = _vehicle()
        t = _base()
        _series(v, t, [12.5, 12.4, 0, 0, 0, 0, 12.3, 12.2, 0])
        first, second = _alerts(v)
        assert (first.get_category_display(), first.get_severity_display()) == ("Main Power Disconnected", "High")
        assert first.occurred_at == t + 2 * 30 * S and first.signal_cleared_at == t + 6 * 30 * S
        assert first.voltage == Decimal("0.00")
        assert second.occurred_at == t + 8 * 30 * S and second.signal_cleared_at is None
        assert Notification.objects.filter(recipient=watcher).count() == 2  # one per disconnection, not per record

    def test_staying_disconnected_is_one_alert(self, watcher):
        v = _vehicle()
        _series(v, _base(), [0] * 12)
        [alert] = _alerts(v)
        assert alert.signal_cleared_at is None
        # (6 min parked with the ignition on is also, correctly, an Idle alert — counted separately)
        assert Notification.objects.filter(alert__category="MAIN_POWER_DISCONNECTED").count() == 1

    def test_normal_voltages_never_alert(self, watcher):
        v = _vehicle()
        _series(v, _base(), [12.5, 24.2, 13.8, 8.5, 12.0])
        assert _alerts(v) == [] and _lows(v) == []

    def test_null_mainpower_is_not_a_disconnection(self, watcher):
        v = _vehicle()
        t = _base()
        _series(v, t, [12.5, None, None, 12.4])
        assert _alerts(v) == []
        _series(v, t + 10 * 30 * S, [0, None, 12.1])
        [alert] = _alerts(v)
        assert alert.signal_cleared_at == t + 12 * 30 * S  # NULL neither restores nor re-opens


class TestVoltageRanges:
    def test_low_voltage_sequence_from_the_spec(self, watcher):
        """12.5, 12.3, 11.8 normal; 7.8 NEW Low; 7.5, 7.2, 6.9, 8.0 same; 8.6 recovered; 8.8 normal."""
        v = _vehicle()
        t = _base()
        _series(v, t, [12.5, 12.3, 11.8, 7.8, 7.5, 7.2, 6.9, 8.0, 8.6, 8.8])
        [low] = _lows(v)
        assert (low.get_category_display(), low.get_severity_display()) == ("Low Voltage", "Medium")
        assert low.occurred_at == t + 3 * 30 * S and low.signal_cleared_at == t + 8 * 30 * S
        assert low.voltage == Decimal("7.80") and low.message == "Low main power voltage (7.80 V)."
        assert _alerts(v) == []
        assert Notification.objects.filter(alert=low).count() == 1

    def test_disconnected_range_sequence_from_the_spec(self, watcher):
        """8.8 normal; 4.5 NEW Disconnected; 3.8, 2.9, 0 same; 12 recovered; 12 normal; 7 NEW Low."""
        v = _vehicle()
        t = _base()
        _series(v, t, [8.8, 4.5, 3.8, 2.9, 0, 12, 12, 7])
        [cut] = _alerts(v)
        [low] = _lows(v)
        assert cut.occurred_at == t + 30 * S and cut.signal_cleared_at == t + 5 * 30 * S
        assert cut.voltage == Decimal("4.50")
        assert low.occurred_at == t + 7 * 30 * S and low.signal_cleared_at is None
        assert Notification.objects.filter(alert__category__in=["MAIN_POWER_DISCONNECTED", "LOW_VOLTAGE"]).count() == 2

    def test_low_to_disconnected_to_low_to_normal_state_machine(self, watcher):
        v = _vehicle()
        t = _base()
        _series(v, t, [12, 7, 3, 6, 9])
        lows, cuts = _lows(v), _alerts(v)
        assert [(a.occurred_at, a.signal_cleared_at) for a in lows] == [
            (t + 30 * S, t + 60 * S), (t + 90 * S, t + 120 * S)]
        assert [(a.occurred_at, a.signal_cleared_at) for a in cuts] == [(t + 60 * S, t + 90 * S)]
        assert Notification.objects.count() == 3  # every change INTO an alert state, nothing else

    @pytest.mark.parametrize("volts, expected", [
        (0, "MAIN_POWER_DISCONNECTED"), (4.99, "MAIN_POWER_DISCONNECTED"), (5.00, "LOW_VOLTAGE"),
        (8.49, "LOW_VOLTAGE"), (8.50, None), (10, None), (12, None),
    ])
    def test_boundaries_end_to_end(self, watcher, volts, expected):
        v = _vehicle()
        _series(v, _base(), [12.6, volts])
        raised = list(Alert.objects.filter(vehicle=v).values_list("category", flat=True))
        assert raised == ([expected] if expected else [])

    def test_vehicles_are_independent(self, watcher):
        a, b, c = _vehicle(), _vehicle(), _vehicle()
        t = _base()
        _series(a, t, [12, 4])
        _series(b, t, [12, 12])
        _series(c, t, [12, 7])
        assert (len(_alerts(a)), len(_lows(a))) == (1, 0)
        assert (len(_alerts(b)), len(_lows(b))) == (0, 0)
        assert (len(_alerts(c)), len(_lows(c))) == (0, 1)


class TestFastNotification:
    def test_pulse_reports_the_newest_unread_alert(self, client, watcher):
        client.force_login(watcher)
        assert client.get("/notifications/pulse/").json() == {"latest_alert": None, "unread_count": 0}
        v = _vehicle()
        t = _base()
        _series(v, t, [12, 7])
        n = Notification.objects.get(recipient=watcher)
        assert client.get("/notifications/pulse/").json() == {"latest_alert": str(n.uuid), "unread_count": 1}
        _series(v, t + 3 * 30 * S, [3])  # Low -> Disconnected: a new alert, a new pulse value
        newest = Notification.objects.filter(recipient=watcher).order_by("-created_at").first()
        assert client.get("/notifications/pulse/").json()["latest_alert"] == str(newest.uuid)

    def test_pulse_is_private_and_needs_login(self, client, watcher):
        _series(_vehicle(), _base(), [12, 4])
        client.force_login(UserFactory())  # no alert permission -> was not notified
        assert client.get("/notifications/pulse/").json()["latest_alert"] is None
        client.logout()
        assert client.get("/notifications/pulse/").status_code == 302

    def test_the_bell_polls_the_pulse(self, client, watcher):
        client.force_login(watcher)
        assert 'data-pulse-url="/notifications/pulse/"' in client.get("/dashboard/").content.decode()


class TestRobustness:
    def test_redelivery_and_restart_never_repeat_the_alert(self, watcher):
        """A restart re-reads current_table (latest value 0 V) every pass: still one alert."""
        v = _vehicle()
        readings = _series(v, _base(), [12.5, 0, 0])
        for _ in range(3):
            TelemetryIngestionService.ingest_events(device=v.tracking_device, events=[readings[-1]])
        assert len(_alerts(v)) == 1 and Notification.objects.count() == 1

    def test_first_ever_reading_at_0_volts_is_an_event_once(self, watcher):
        v = _vehicle()
        _series(v, _base(), [0, 0])
        assert len(_alerts(v)) == 1

    def test_out_of_order_records(self, watcher):
        v = _vehicle()
        t = _base()
        readings = [_r(t + k * 30 * S, volts, i=k) for k, volts in enumerate([12.5, 0, 0, 12.4])]
        for k in (3, 1, 0, 2):
            TelemetryIngestionService.ingest_events(device=v.tracking_device, events=[readings[k]])
        [alert] = _alerts(v)
        assert alert.occurred_at == t + 30 * S and alert.signal_cleared_at == t + 90 * S

    def test_state_is_per_vehicle(self, watcher):
        a, b = _vehicle(), _vehicle()
        t = _base()
        _series(a, t, [12.5, 0, 0])
        _series(b, t, [12.5, 0])
        _series(a, t + 5 * 30 * S, [12.6])  # A restored; B still disconnected
        assert len(_alerts(a)) == 1 and len(_alerts(b)) == 1
        assert _alerts(a)[0].signal_cleared_at is not None and _alerts(b)[0].signal_cleared_at is None

    def test_old_history_does_not_notify(self, watcher):
        v = _vehicle()
        _series(v, timezone.now() - datetime.timedelta(days=3), [12.5, 0])
        assert len(_alerts(v)) == 1 and not Notification.objects.exists()


class TestIndependence:
    def test_panic_overspeed_idle_and_power_coexist(self, watcher):
        v = _vehicle()
        t = timezone.now() - datetime.timedelta(minutes=20)
        readings = [_r(t + k * 30 * S, 0 if 3 <= k <= 6 else 12.6, i=0, panic=1 if k == 2 else 0) for k in range(13)]
        readings[8].metadata["overspeed"] = 1
        TelemetryIngestionService.ingest_events(device=v.tracking_device, events=readings)
        kinds = dict(Alert.objects.filter(vehicle=v).values_list("category", "severity"))
        assert kinds == {"PANIC": "CRITICAL", "MAIN_POWER_DISCONNECTED": "HIGH", "OVER_SPEEDING": "HIGH",
                         "IDLE": "MEDIUM"}
        panic = Alert.objects.get(vehicle=v, category="PANIC")
        assert panic.signal_cleared_at == t + 3 * 30 * S  # panic's own 0, not the power readings


class TestNotificationAndReport:
    def test_notification(self, client, watcher):
        v = _vehicle(registration_number="KA01AB1234")
        _series(v, _base(), [12.5, 0])
        n = Notification.objects.get(recipient=watcher)
        assert n.title == "Main Power Disconnected alert — KA01AB1234"
        assert "Level: High" in n.body and "Main power disconnected (0.00 V)." in n.body
        client.force_login(watcher)
        [announced] = client.get("/notifications/feed/").json()["alerts"]
        assert (announced["type_label"], announced["level_label"], announced["severity"]) == (
            "Main Power Disconnected", "High", "HIGH")

    def test_report_filter_and_exports(self, client):
        from openpyxl import load_workbook

        v = _vehicle()
        _series(v, timezone.now() - datetime.timedelta(minutes=20), [12.5, 0, 0, 12.4])
        client.force_login(UserFactory(role=role_with((Permission.Module.ALERT, Permission.Action.VIEW))))
        rows = client.get("/api/v1/alerts/report/", {"alert_type": "MAIN_POWER_DISCONNECTED"}).json()["alerts"]["rows"]
        assert [(r["type_label"], r["level_label"], r["voltage"]) for r in rows] == [
            ("Main Power Disconnected", "High", 0.0)]
        assert rows[0]["duration_text"] == "1m 00s"
        xlsx = client.get("/api/v1/alerts/report/export/", {"type": "xlsx", "alert_type": "MAIN_POWER_DISCONNECTED"})
        header, *data = list(load_workbook(io.BytesIO(xlsx.content))["Alerts"].iter_rows(values_only=True))
        values = dict(zip(header, data[0]))
        assert (values["Alert"], values["Level"]) == ("Main Power Disconnected", "High")
        pdf = client.get("/api/v1/alerts/report/export/", {"type": "pdf", "alert_type": "MAIN_POWER_DISCONNECTED"})
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


class TestLiveTracking:
    def test_main_power_reaches_the_vehicle_status_feed(self, client):
        v = _vehicle()
        _series(v, _base(), [12.676])
        assert VehicleCurrentTelemetry.objects.get(vehicle=v).main_power == Decimal("12.676")
        client.force_login(UserFactory(role=role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW))))
        [item] = client.get("/api/v1/tracking/fleet/current/").json()["results"]
        assert (item["main_power"], item["main_power_state"]) == ("12.676", "NORMAL")
        _series(v, _base() + 60 * S, [6.5])
        [item] = client.get("/api/v1/tracking/fleet/current/").json()["results"]
        assert (item["main_power"], item["main_power_state"]) == ("6.500", "LOW")
        _series(v, _base() + 120 * S, [0])
        [item] = client.get("/api/v1/tracking/fleet/current/").json()["results"]
        assert (item["main_power"], item["main_power_state"]) == ("0.000", "DISCONNECTED")

    def test_live_page_has_the_main_power_column(self, client):
        client.force_login(UserFactory(role=role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW))))
        html = client.get("/tracking/live/").content.decode()
        assert "<th>Main Power</th>" in html


class TestCommsBridgeEndToEnd:
    def test_comms_rows_raise_one_alert_per_disconnection(self, watcher):
        v = _vehicle()
        v.tracking_device.imei = IMEI
        v.tracking_device.save(update_fields=["imei"])
        now_local = timezone.now().astimezone(comms_sync._local_timezone()).replace(tzinfo=None, microsecond=0)
        t = now_local - datetime.timedelta(minutes=5)
        volts = [12.69, 0.0, 0.0, 12.68, 12.68]
        rows = [{"id": k + 1, "unitno": IMEI, "tracktime": t + k * 30 * S, "lat": 12.98, "lon": 77.53 + k * 1e-5,
                 "speed": 0.0, "direction": "0", "ignition": 1, "odometer": 1000, "gpsodometer": 0, "gpsstatus": 1,
                 "location": "Bengaluru", "mainpower": mv} for k, mv in enumerate(volts)]
        comms_sync.apply_rows(rows, comms_sync.SyncReport())
        [alert] = _alerts(v)
        assert alert.signal_cleared_at is not None
        assert Notification.objects.filter(recipient=watcher).count() == 1
        assert TelemetryEvent.objects.filter(vehicle=v, external_power=False).count() == 2
