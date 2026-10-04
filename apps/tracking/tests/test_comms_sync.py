"""The comms bridge: comms App DB rows -> FMS tracking tables, mapped by IMEI
-> vehicle -> client. Uses an in-memory fake of the psycopg connection so the
tests never touch a real second database."""

import datetime
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse

from apps.accounts.models import Permission
from apps.accounts.tests.factories import UserFactory
from apps.clients.tests.factories import ClientFactory
from apps.core.tests.factories import role_with
from apps.tracking import comms_sync
from apps.tracking.models import CommsSyncState, TelemetryEvent, TrackingDevice, VehicleCurrentTelemetry
from apps.tracking.tests.factories import TrackingDeviceFactory
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db

IST = ZoneInfo("Asia/Kolkata")
IMEI = "350424067315497"


def _row(unit=IMEI, id_=1, minute=0, when=None, lat=12.9854033, lon=77.53954, **extra):
    row = {
        "id": id_,
        "unitno": unit,
        "tracktime": when or datetime.datetime(2026, 9, 20, 17, minute, 14),
        "lat": lat,
        "lon": lon,
        "speed": 0.0,
        "direction": "90.0",
        "ignition": 1,
        "odometer": 293123,
        "gpsodometer": 454,
        "gpsstatus": 1,
        "location": "Bengaluru",
    }
    row.update(extra)
    return row


class FakeConn:
    """Answers the queries comms_sync issues against the comms App DB."""

    def __init__(self, current=(), history=()):
        self.current, self.history = list(current), list(history)

    def execute(self, sql, params=None):
        outer = self

        class Result:
            def fetchall(self):
                if "FROM current_table" in sql:
                    return outer.current
                if "FROM history_table WHERE id >" in sql:
                    after, limit = params
                    return [r for r in outer.history if r["id"] > after][:limit]
                raise AssertionError(sql)

            def fetchone(self):
                if "min(id)" in sql:
                    ids = [r["id"] for r in outer.history if r["tracktime"] >= params[0]]
                    return {"m": min(ids) if ids else None}
                if "max(id)" in sql:
                    return {"m": max([r["id"] for r in outer.history] or [0])}
                raise AssertionError(sql)

        return Result()


def _view_role():
    return role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW))


def _owned_device(client_obj=None, imei=IMEI):
    client_obj = client_obj or ClientFactory()
    vehicle = VehicleFactory(client=client_obj)
    TrackingDeviceFactory(imei=imei, vehicle=vehicle)
    return client_obj, vehicle


class TestRowToEvent:
    def test_converts_units_and_timezone(self):
        event = comms_sync.row_to_event(_row(minute=0), IST)
        # 17:00:14 India time is 11:30:14 UTC
        assert event.timestamp.astimezone(datetime.timezone.utc).strftime("%H:%M:%S") == "11:30:14"
        assert str(event.odometer) == "293.1"  # metres -> km
        assert event.heading == 90 and event.ignition is True
        assert event.metadata["source"] == "comms" and event.metadata["location"] == "Bengaluru"

    def test_rows_without_a_position_or_time_are_dropped(self):
        assert comms_sync.row_to_event(_row(lat=None), IST) is None
        assert comms_sync.row_to_event(_row(tracktime=None), IST) is None

    def test_garbage_direction_is_ignored_not_fatal(self):
        assert comms_sync.row_to_event(_row(direction="n/a"), IST).heading is None


class TestApplyRows:
    def test_reading_lands_on_the_vehicle_and_client_registered_in_fms(self):
        client_obj, vehicle = _owned_device()
        report = comms_sync.SyncReport()
        comms_sync.apply_rows([_row(id_=1, minute=0), _row(id_=2, minute=1)], report, IST)
        assert report.accepted == 2
        events = TelemetryEvent.objects.all()
        assert events.count() == 2
        assert {e.client_id for e in events} == {client_obj.id}
        current = VehicleCurrentTelemetry.objects.get(vehicle=vehicle)
        assert current.timestamp.astimezone(datetime.timezone.utc).minute == 31  # newest reading, stored in UTC

    def test_two_clients_two_devices_stay_separate(self):
        a, va = _owned_device(imei="111111111111111")
        b, vb = _owned_device(imei="222222222222222")
        rows = [_row("111111111111111"), _row("222222222222222", id_=2)]
        comms_sync.apply_rows(rows, comms_sync.SyncReport(), IST)
        assert TelemetryEvent.objects.get(vehicle=va).client == a
        assert TelemetryEvent.objects.get(vehicle=vb).client == b

    def test_unknown_imei_is_skipped_and_reported_never_auto_created(self):
        report = comms_sync.SyncReport()
        comms_sync.apply_rows([_row("999999999999999")], report, IST)
        assert dict(report.unknown_units) == {"999999999999999": 1}
        assert not TelemetryEvent.objects.exists()
        assert not TrackingDevice.objects.exists()

    def test_inactive_device_is_skipped_and_reported(self):
        TrackingDeviceFactory(imei=IMEI, vehicle=VehicleFactory(), status=TrackingDevice.Status.SUSPENDED)
        report = comms_sync.SyncReport()
        comms_sync.apply_rows([_row()], report, IST)
        assert dict(report.inactive_units) == {IMEI: 1}
        assert not TelemetryEvent.objects.exists()

    def test_device_without_a_vehicle_keeps_history_but_has_no_owner(self):
        TrackingDeviceFactory(imei=IMEI, vehicle=None)
        comms_sync.apply_rows([_row()], comms_sync.SyncReport(), IST)
        event = TelemetryEvent.objects.get()
        assert event.vehicle is None and event.client is None
        assert not VehicleCurrentTelemetry.objects.exists()

    def test_reapplying_the_same_rows_does_not_duplicate_history(self):
        _owned_device()
        for _ in range(3):
            comms_sync.apply_rows([_row(id_=1)], comms_sync.SyncReport(), IST)
        assert TelemetryEvent.objects.count() == 1

    def test_comms_own_client_and_vehicle_columns_are_ignored(self):
        """The old comms tables carry their own clientid/vehicleno; FMS master data wins."""
        client_obj, vehicle = _owned_device()
        comms_sync.apply_rows([_row(clientid="5", vehicleno="SOMETHING-ELSE")], comms_sync.SyncReport(), IST)
        event = TelemetryEvent.objects.get()
        assert event.vehicle == vehicle and event.client == client_obj


class TestLocationColumn:
    def test_address_from_comms_lands_on_the_current_position_and_the_fleet_feed(self, client):
        client_obj, vehicle = _owned_device()
        comms_sync.apply_rows([_row(location="NH48, Nelamangala, Bengaluru")], comms_sync.SyncReport(), IST)
        assert VehicleCurrentTelemetry.objects.get(vehicle=vehicle).location == "NH48, Nelamangala, Bengaluru"

        client.force_login(UserFactory(role=_view_role(), client=client_obj))
        item = client.get(reverse("fleet-telemetry-current")).json()["results"][0]
        assert item["location"] == "NH48, Nelamangala, Bengaluru"

    def test_a_newer_reading_replaces_the_address(self):
        _, vehicle = _owned_device()
        comms_sync.apply_rows([_row(id_=1, minute=0, location="Old Rd")], comms_sync.SyncReport(), IST)
        comms_sync.apply_rows([_row(id_=2, minute=5, location="New Rd")], comms_sync.SyncReport(), IST)
        assert VehicleCurrentTelemetry.objects.get(vehicle=vehicle).location == "New Rd"

    def test_address_is_filled_in_for_a_position_that_was_stored_without_one(self):
        _, vehicle = _owned_device()
        comms_sync.apply_rows([_row(location="")], comms_sync.SyncReport(), IST)
        assert VehicleCurrentTelemetry.objects.get(vehicle=vehicle).location == ""
        comms_sync.apply_rows([_row(location="MG Road")], comms_sync.SyncReport(), IST)  # same timestamp
        assert VehicleCurrentTelemetry.objects.get(vehicle=vehicle).location == "MG Road"

    def test_readings_without_an_address_leave_it_blank_not_null(self):
        _, vehicle = _owned_device()
        comms_sync.apply_rows([_row(location=None)], comms_sync.SyncReport(), IST)
        assert VehicleCurrentTelemetry.objects.get(vehicle=vehicle).location == ""


class TestGpsOdometerAndTrackTime:
    def test_gps_odometer_is_converted_to_km_and_stored_on_the_current_position(self, client):
        client_obj, vehicle = _owned_device()
        comms_sync.apply_rows([_row(gpsodometer=12345)], comms_sync.SyncReport(), IST)
        current = VehicleCurrentTelemetry.objects.get(vehicle=vehicle)
        assert str(current.gps_odometer) == "12.345"

        client.force_login(UserFactory(role=_view_role(), client=client_obj))
        item = client.get(reverse("fleet-telemetry-current")).json()["results"][0]
        assert item["gps_odometer"] == "12.345"
        # track time = the device's own timestamp (17:00:14 IST), served as an ISO instant
        assert item["timestamp"].startswith("2026-09-20T11:30:14")

    def test_missing_gps_odometer_stays_null(self):
        _, vehicle = _owned_device()
        comms_sync.apply_rows([_row(gpsodometer=None)], comms_sync.SyncReport(), IST)
        assert VehicleCurrentTelemetry.objects.get(vehicle=vehicle).gps_odometer is None

    def test_gps_odometer_is_filled_in_for_a_position_stored_before_it_was_carried_over(self):
        _, vehicle = _owned_device()
        comms_sync.apply_rows([_row(gpsodometer=None)], comms_sync.SyncReport(), IST)
        comms_sync.apply_rows([_row(gpsodometer=454)], comms_sync.SyncReport(), IST)  # same timestamp
        assert str(VehicleCurrentTelemetry.objects.get(vehicle=vehicle).gps_odometer) == "0.454"


class TestSync:
    def test_first_run_imports_current_then_history_and_saves_the_cursor(self):
        _owned_device()
        history = [_row(id_=i, minute=i) for i in range(1, 6)]
        conn = FakeConn(current=[_row(id_=5, minute=5)], history=history)
        report = comms_sync.sync(connection=conn, history_days=36500)
        assert report.current_rows == 1 and report.history_rows == 5
        assert CommsSyncState.objects.get(key=comms_sync.STATE_KEY).last_id == 5
        assert VehicleCurrentTelemetry.objects.count() == 1

    def test_second_run_only_reads_new_rows(self):
        _owned_device()
        conn = FakeConn(current=[_row(id_=3, minute=3)], history=[_row(id_=i, minute=i) for i in range(1, 4)])
        comms_sync.sync(connection=conn, history_days=36500)
        conn.history.append(_row(id_=4, minute=4))
        report = comms_sync.sync(connection=conn)
        assert report.history_rows == 1
        assert CommsSyncState.objects.get(key=comms_sync.STATE_KEY).last_id == 4

    def test_first_run_skips_history_older_than_the_window(self):
        _owned_device()
        now = datetime.datetime.now()
        old = _row(id_=1, when=now - datetime.timedelta(days=400))
        recent = _row(id_=2, when=now - datetime.timedelta(hours=1))
        report = comms_sync.sync(connection=FakeConn(history=[old, recent]), history_days=7)
        assert report.history_rows == 1

    def test_a_failing_source_records_the_error_and_raises(self):
        class Broken(FakeConn):
            def execute(self, sql, params=None):
                raise RuntimeError("comms db is down")

        with pytest.raises(RuntimeError):
            comms_sync.sync(connection=Broken())
        assert "comms db is down" in CommsSyncState.objects.get(key=comms_sync.STATE_KEY).last_error

    def test_synced_vehicle_shows_on_the_fleet_map_only_for_its_own_client(self, client):
        client_obj, _vehicle = _owned_device()
        owner = UserFactory(role=_view_role(), client=client_obj)
        stranger = UserFactory(role=_view_role(), client=ClientFactory())
        comms_sync.sync(connection=FakeConn(current=[_row()]), history_days=7)

        client.force_login(owner)
        assert client.get(reverse("fleet-telemetry-current")).json()["count"] == 1
        client.force_login(stranger)
        assert client.get(reverse("fleet-telemetry-current")).json()["count"] == 0


def test_command_explains_itself_when_the_bridge_is_not_configured(settings):
    settings.COMMS_APP_DATABASE_URL = ""
    with pytest.raises(CommandError, match="COMMS_APP_DATABASE_URL"):
        call_command("sync_comms_data")
