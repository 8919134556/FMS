"""Panic alert EVENTS (apps.alerts.events): one alert per panic episode per
vehicle, raised from the comms ``panic`` flag carried in the GPS history, with
one notification per allowed user — through the real ingestion path."""

import datetime
from decimal import Decimal
from unittest import mock

import pytest
from django.utils import timezone

from apps.accounts.models import Permission
from apps.accounts.tests.factories import UserFactory
from apps.alerts import events
from apps.alerts.models import Alert
from apps.clients.tests.factories import ClientFactory
from apps.core.tests.factories import role_with
from apps.drivers.tests.factories import DriverFactory
from apps.notifications.models import Notification
from apps.tracking import comms_sync
from apps.tracking.models import TelemetryEvent
from apps.tracking.providers.base import NormalizedEvent
from apps.tracking.services import TelemetryIngestionService
from apps.tracking.tests.factories import TrackingDeviceFactory
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db

S = datetime.timedelta(seconds=1)


def _base():
    return timezone.now() - datetime.timedelta(minutes=30)


def _vehicle(client_obj=None, **kwargs):
    vehicle = VehicleFactory(client=client_obj or ClientFactory(), **kwargs)
    TrackingDeviceFactory(vehicle=vehicle)
    return vehicle


def _reading(when, panic, *, lat="12.985287", voltage=None, location="Basaveshwaranagar, Bengaluru", **extra):
    metadata = {"source": "comms", "location": location}
    if panic is not None:
        metadata["panic"] = panic
    if voltage is not None:
        metadata["panic_voltage"] = voltage
    return NormalizedEvent(
        timestamp=when, latitude=Decimal(lat), longitude=Decimal("77.539518"), speed=Decimal("12.50"),
        ignition=True, odometer=Decimal("108605.1"), metadata=metadata, **extra,
    )


def _ingest(vehicle, *readings):
    """The comms bridge path: one batch per device, already normalized."""
    return TelemetryIngestionService.ingest_events(device=vehicle.tracking_device, events=list(readings))


def _signals(vehicle, start, values, step=2 * S):
    """One batch: readings ``step`` apart with these panic values (None = no info)."""
    readings = [_reading(start + i * step, v, lat=f"12.98{5000 + i:04d}") for i, v in enumerate(values)]
    _ingest(vehicle, *readings)
    return readings


def _panics(vehicle):
    return list(Alert.objects.filter(category=Alert.Category.PANIC, vehicle=vehicle).order_by("occurred_at"))


@pytest.fixture
def watcher():
    """A staff user who may see alerts — receives the notifications."""
    return UserFactory(role=role_with((Permission.Module.ALERT, Permission.Action.VIEW)))


class TestUserScenarios:
    def test_1_normal_data_raises_nothing(self, watcher):
        v = _vehicle()
        _signals(v, _base(), [0, 0, 0])
        assert _panics(v) == []
        assert not Notification.objects.exists()

    def test_2_new_panic_is_one_alert_and_one_notification(self, watcher):
        v = _vehicle()
        t = _base()
        _signals(v, t, [0, 1])
        [alert] = _panics(v)
        assert alert.occurred_at == t + 2 * S and alert.signal_cleared_at is None
        assert alert.status == Alert.Status.OPEN and alert.severity == Alert.Severity.CRITICAL
        assert Notification.objects.filter(recipient=watcher, alert=alert).count() == 1

    def test_3_continuous_panic_is_one_event(self, watcher):
        v = _vehicle()
        t = _base()
        _signals(v, t, [1, 1, 1, 1])
        [alert] = _panics(v)
        assert alert.occurred_at == t and alert.last_signal_at == t + 6 * S
        assert Notification.objects.count() == 1

    def test_3b_continuous_panic_across_separate_batches_is_still_one_event(self, watcher):
        v = _vehicle()
        t = _base()
        for i in range(4):
            _ingest(v, _reading(t + i * 5 * S, 1, lat=f"12.98{6000 + i:04d}"))
        assert len(_panics(v)) == 1
        assert Notification.objects.count() == 1

    def test_4_cleared_panic_stays_in_history(self, watcher):
        v = _vehicle()
        t = _base()
        _signals(v, t, [1, 1, 0])
        [alert] = _panics(v)
        assert alert.signal_cleared_at == t + 4 * S
        assert alert.status == Alert.Status.OPEN  # cleared signal != a human resolving it

    def test_5_new_panic_after_clearing_is_a_second_event(self, watcher):
        v = _vehicle()
        t = _base()
        _signals(v, t, [1, 1, 0, 0, 1])
        first, second = _panics(v)
        assert first.occurred_at == t and first.signal_cleared_at == t + 4 * S
        assert second.occurred_at == t + 8 * S and second.signal_cleared_at is None
        assert Notification.objects.count() == 2

    def test_6_state_is_per_vehicle(self, watcher):
        a, b = _vehicle(), _vehicle()
        t = _base()
        _signals(a, t, [1])
        _signals(b, t, [1])
        assert len(_panics(a)) == 1 and len(_panics(b)) == 1
        _signals(a, t + 10 * S, [0])  # A clearing does not touch B
        assert _panics(a)[0].signal_cleared_at is not None
        assert _panics(b)[0].signal_cleared_at is None


class TestArrivalOrder:
    def test_a_clear_that_arrives_before_its_panic_still_clears_it(self, watcher):
        """Real comms data: 21:49:47 (0) was stored before 21:49:44 (1)."""
        v = _vehicle()
        t = _base()
        _ingest(v, _reading(t + 3 * S, 0, lat="12.985001"))
        _ingest(v, _reading(t, 1, lat="12.985002"))
        [alert] = _panics(v)
        assert alert.occurred_at == t and alert.signal_cleared_at == t + 3 * S

    def test_a_late_earlier_panic_reading_moves_the_start_back_without_a_new_alert(self, watcher):
        v = _vehicle()
        t = _base()
        _ingest(v, _reading(t + 3 * S, 1, lat="12.985003", voltage="12.40"))
        _ingest(v, _reading(t, 1, lat="12.985000", voltage="11.86"))
        [alert] = _panics(v)
        assert alert.occurred_at == t and alert.voltage == Decimal("11.86")
        assert Notification.objects.count() == 1

    def test_unsorted_batch_is_judged_in_device_time(self, watcher):
        v = _vehicle()
        t = _base()
        _ingest(v, _reading(t + 4 * S, 1, lat="12.985004"), _reading(t, 1, lat="12.985000"),
                _reading(t + 2 * S, 0, lat="12.985002"))
        first, second = _panics(v)
        assert first.occurred_at == t and first.signal_cleared_at == t + 2 * S
        assert second.occurred_at == t + 4 * S

    def test_redelivered_readings_change_nothing(self, watcher):
        v = _vehicle()
        t = _base()
        batch = [_reading(t, 1, lat="12.985000"), _reading(t + 2 * S, 1, lat="12.985001")]
        _ingest(v, *batch)
        _ingest(v, *batch)
        assert len(_panics(v)) == 1 and Notification.objects.count() == 1

    def test_readings_without_panic_information_never_change_state(self, watcher):
        v = _vehicle()
        t = _base()
        _signals(v, t, [1, None, None, 1])
        [alert] = _panics(v)
        assert alert.signal_cleared_at is None and alert.last_signal_at == t + 6 * S
        _signals(v, t + 20 * S, [None])
        assert len(_panics(v)) == 1


class TestEventContent:
    def test_alert_snapshots_the_reading_that_raised_it(self, watcher):
        client_obj = ClientFactory()
        driver = DriverFactory()
        v = _vehicle(client_obj, current_driver=driver)
        t = _base()
        _ingest(v, _reading(t, 1, voltage="12.37", location="5th Cross Road, Bengaluru"))
        [alert] = _panics(v)
        source = TelemetryEvent.objects.get(vehicle=v)
        assert alert.telemetry_event == source
        assert (alert.client, alert.driver, alert.location) == (client_obj, driver, "5th Cross Road, Bengaluru")
        assert alert.latitude == Decimal("12.985287") and alert.speed == Decimal("12.50")
        assert alert.ignition is True and alert.odometer == Decimal("108605.1") and alert.voltage == Decimal("12.37")
        assert alert.link_url.endswith(f"?alert={alert.uuid}")

    def test_no_gps_fix_keeps_coordinates_empty(self, watcher):
        v = _vehicle()
        _ingest(v, NormalizedEvent(timestamp=_base(), latitude=Decimal("0"), longitude=Decimal("0"),
                                   metadata={"panic": 1}))
        [alert] = _panics(v)
        assert alert.latitude is None and alert.longitude is None


class TestNotifications:
    def test_only_users_who_may_see_the_vehicle_are_notified(self):
        a, b = ClientFactory(), ClientFactory()
        view = role_with((Permission.Module.ALERT, Permission.Action.VIEW))
        staff = UserFactory(role=view)
        client_a_user = UserFactory(role=view, client=a)
        client_b_user = UserFactory(role=view, client=b)
        no_permission = UserFactory(role=role_with((Permission.Module.VEHICLE, Permission.Action.VIEW)))
        inactive = UserFactory(role=view, status="INACTIVE")
        superuser = UserFactory(is_superuser=True)
        v = _vehicle(a)
        _signals(v, _base(), [1])
        notified = set(Notification.objects.values_list("recipient_id", flat=True))
        assert notified == {staff.pk, client_a_user.pk, superuser.pk}
        assert client_b_user.pk not in notified and no_permission.pk not in notified and inactive.pk not in notified

    def test_notification_identifies_the_panic(self, watcher):
        v = _vehicle(registration_number="KA01AB1234", current_driver=DriverFactory(first_name="John", last_name="Rao"))
        _ingest(v, _reading(_base(), 1, location="MG Road, Bengaluru"))
        n = Notification.objects.get(recipient=watcher)
        assert n.title == "Panic alert — KA01AB1234"
        assert "Driver: John Rao" in n.body and "MG Road, Bengaluru" in n.body
        assert n.level == Notification.Level.CRITICAL and n.alert == _panics(v)[0]

    def test_old_events_from_a_history_import_do_not_notify(self, watcher):
        v = _vehicle()
        _signals(v, timezone.now() - datetime.timedelta(days=3), [1])
        assert len(_panics(v)) == 1
        assert not Notification.objects.exists()


class TestRobustness:
    def test_an_alerting_failure_never_loses_the_telemetry(self, watcher):
        v = _vehicle()
        with mock.patch.object(events, "_on_level", side_effect=RuntimeError("boom")):
            result = _ingest(v, _reading(_base(), 1))
        assert result.accepted == 1
        assert TelemetryEvent.objects.filter(vehicle=v).count() == 1
        assert _panics(v) == []

    def test_unmapped_device_raises_no_alert(self, watcher):
        device = TrackingDeviceFactory(vehicle=None)
        TelemetryIngestionService.ingest_events(device=device, events=[_reading(_base(), 1)])
        assert not Alert.objects.filter(category=Alert.Category.PANIC).exists()


class TestCommsBridge:
    def _row(self, **extra):
        row = {"id": 1, "unitno": "350424067315497", "tracktime": datetime.datetime(2026, 10, 2, 21, 49, 44),
               "lat": 12.9852866, "lon": 77.5395183, "speed": 0.0, "direction": "0", "ignition": 1,
               "odometer": 108605074, "gpsodometer": 0, "gpsstatus": 1, "location": "Bengaluru"}
        row.update(extra)
        return row

    def test_the_comms_panic_flag_is_carried_unchanged(self):
        tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        assert comms_sync.row_to_event(self._row(panic=1), tz).metadata["panic"] == 1
        assert comms_sync.row_to_event(self._row(panic=0), tz).metadata["panic"] == 0
        assert "panic" not in comms_sync.row_to_event(self._row(panic=None), tz).metadata
        event = comms_sync.row_to_event(self._row(panic=1, panic_voltage="12.37"), tz)
        assert event.metadata["panic_voltage"] == "12.37"

    def test_panic_voltage_is_read_from_the_raw_db_for_panic_rows_only(self):
        rows = [self._row(panic=1), self._row(id=2, panic=0, tracktime=datetime.datetime(2026, 10, 2, 21, 49, 47))]

        class Raw:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params):
                assert params[0] == ["350424067315497"]  # only the panic row is looked up

                class Result:
                    def fetchall(self):
                        return [{"unitno": "350424067315497", "tracktime": rows[0]["tracktime"], "analog1": 12371.0}]
                return Result()

        with mock.patch.object(comms_sync, "_connect", return_value=Raw()):
            comms_sync.attach_panic_voltage(rows, raw_dsn="postgres://raw")
        assert rows[0]["panic_voltage"] == "12.37" and "panic_voltage" not in rows[1]

    def test_without_a_raw_db_voltage_is_simply_absent(self):
        rows = [self._row(panic=1)]
        comms_sync.attach_panic_voltage(rows, raw_dsn="")
        assert "panic_voltage" not in rows[0]

    def test_a_raw_db_failure_does_not_stop_the_sync(self):
        rows = [self._row(panic=1)]
        with mock.patch.object(comms_sync, "_connect", side_effect=OSError("down")):
            comms_sync.attach_panic_voltage(rows, raw_dsn="postgres://raw")
        assert "panic_voltage" not in rows[0]

    def test_bridge_rows_raise_one_alert_per_episode(self, watcher):
        v = _vehicle()
        device = v.tracking_device
        device.imei = "350424067315497"
        device.save(update_fields=["imei"])
        now_local = timezone.now().astimezone(comms_sync._local_timezone()).replace(tzinfo=None, microsecond=0)
        start = now_local - datetime.timedelta(minutes=5)
        rows = [self._row(id=i + 1, tracktime=start + i * 2 * S, lat=12.98528 + i / 1e5, panic=p)
                for i, p in enumerate([0, 1, 1, 1, 0, 0, 1])]
        comms_sync.apply_rows(rows, comms_sync.SyncReport())
        assert len(_panics(v)) == 2
        assert Notification.objects.filter(recipient=watcher).count() == 2
