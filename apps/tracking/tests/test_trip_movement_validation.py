"""Trip START validation: ignition ON is only a CANDIDATE trip; a trip exists
only once the history shows genuine movement (apps.tracking.trip_report,
rules A/B/C), and it starts where the vehicle departed — never at ignition ON
just because the ignition came on. The trip END (closure time) is unchanged."""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Permission
from apps.accounts.tests.factories import UserFactory
from apps.core.tests.factories import role_with
from apps.tracking import trip_report
from apps.tracking.tests.factories import TelemetryEventFactory, TrackingDeviceFactory
from apps.vehicles.models import Vehicle
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db

S = datetime.timedelta(seconds=1)
M = datetime.timedelta(minutes=1)
LAT0, LON0 = 12.971600, 77.594600
STEP = 0.0001  # ~11 m of latitude


def _base():
    return timezone.now().replace(hour=9, minute=0, second=0, microsecond=0) - datetime.timedelta(days=1)


def _vehicle(**settings):
    vehicle = VehicleFactory(**settings)
    TrackingDeviceFactory(vehicle=vehicle)
    return vehicle


def _r(vehicle, when, *, speed=0, ignition=True, dlat=0.0, odometer=None, lat=None, lon=None, metadata=None):
    TelemetryEventFactory(
        device=vehicle.tracking_device, vehicle=vehicle, timestamp=when, ignition=ignition, speed=speed,
        odometer=odometer, latitude=f"{(lat if lat is not None else LAT0 + dlat):.6f}",
        longitude=f"{(lon if lon is not None else LON0):.6f}", metadata=metadata or {},
    )


def _trips(vehicle, now=None):
    now = now or _base() + datetime.timedelta(hours=3)
    trips, _ = trip_report.compute_vehicle_trips(
        user=None, start_date=_base().date(), end_date=now.date(), vehicle_uuid=str(vehicle.uuid), now=now
    )
    return sorted(trips, key=lambda t: t.start_at)


def _session(vehicle, t, speeds, *, every=10 * S, dlats=None, odometers=None, off=True):
    """Ignition ON, one reading per speed (``every`` apart), then ignition OFF."""
    for i, speed in enumerate(speeds):
        _r(vehicle, t + i * every, speed=speed, dlat=dlats[i] if dlats else 0.0,
           odometer=odometers[i] if odometers else None)
    end = t + len(speeds) * every
    if off:
        _r(vehicle, end, ignition=False, dlat=dlats[-1] if dlats else 0.0, odometer=odometers[-1] if odometers else None)
    return end


class TestUserScenarios:
    def test_1_ignition_on_but_never_moves_is_no_trip(self):
        v = _vehicle()
        _session(v, _base(), [0, 0, 0, 0])
        assert _trips(v) == []

    def test_2_one_abnormal_speed_reading_is_no_trip(self):
        v = _vehicle()
        _session(v, _base(), [0, 0, 18, 0, 0], dlats=[0, 0.00002, 0, 0.00001, 0])  # GPS drift of a couple of metres
        assert _trips(v) == []

    def test_3_real_departure_is_a_trip_starting_where_movement_began(self):
        v = _vehicle()
        t = _base()
        speeds = [0, 8, 15, 22, 25, 20]
        _session(v, t, speeds, dlats=[0, STEP, 3 * STEP, 6 * STEP, 10 * STEP, 14 * STEP])
        trips = _trips(v)
        assert len(trips) == 1
        assert trips[0].start_at == t + 10 * S  # the first moving reading — not the ignition-ON reading

    def test_4_slow_but_sustained_movement_is_a_trip(self):
        v = _vehicle()
        _session(v, _base(), [3, 5, 7, 9, 6], every=15 * S, dlats=[0, STEP, 2 * STEP, 4 * STEP, 6 * STEP])
        assert len(_trips(v)) == 1


class TestEdgeCases:
    def test_moves_stops_briefly_and_continues_is_one_trip(self):
        v = _vehicle()
        t = _base()
        dl = [i * 2 * STEP for i in range(5)]
        _session(v, t, [20, 25, 30, 25, 20], dlats=dl, off=False)
        for i in range(6):  # 1 minute stationary, ignition still on (traffic light)
            _r(v, t + 50 * S + i * 10 * S, speed=0, dlat=dl[-1])
        _session(v, t + 110 * S, [20, 30, 30], dlats=[dl[-1] + (i + 1) * 2 * STEP for i in range(3)])
        assert len(_trips(v)) == 1

    def test_long_idle_before_departure_is_not_counted_in_the_trip(self):
        """Ignition on for 10 min parked (dense 2 s reporting: 300 readings — the
        whole first fetch pass), then it drives: the trip starts at the drive."""
        v = _vehicle()
        t = _base()
        for i in range(300):
            _r(v, t + i * 2 * S, speed=0, dlat=0.00001 * (i % 3))  # parked, tiny GPS jitter
        depart = t + 600 * S
        _session(v, depart, [12, 20, 28, 30, 30], dlats=[(i + 1) * 3 * STEP for i in range(5)])
        trips = _trips(v)
        assert len(trips) == 1
        assert trips[0].start_at == depart
        assert trips[0].duration(now=depart + M) < 2 * M  # the 10 parked minutes are excluded

    def test_brief_movement_below_the_evidence_is_no_trip_and_enough_is_a_trip(self):
        brief = _vehicle()
        _session(brief, _base(), [0, 10, 12, 0, 0], dlats=[0, STEP, 2 * STEP, 2 * STEP, 2 * STEP])  # 2 moving, 22 m
        assert _trips(brief) == []
        enough = _vehicle()
        _session(enough, _base(), [0, 10, 12, 14, 0], dlats=[0, 2 * STEP, 4 * STEP, 6 * STEP, 6 * STEP])
        assert len(_trips(enough)) == 1


class TestDevicesWithoutSpeed:
    """Real pattern in this fleet: speed is reported as 0 while driving, and
    some trips have only an ignition-ON and an ignition-OFF reading."""

    def test_gps_displacement_corroborated_by_the_odometer_is_a_trip(self):
        v = _vehicle()
        t = _base()
        _r(v, t, speed=0, odometer=57.7)
        _r(v, t + 351 * S, ignition=False, speed=0, dlat=0.013, odometer=59.7)  # 1.4 km away, +2.0 km
        trips = _trips(v)
        assert len(trips) == 1 and trips[0].start_at == t and trips[0].distance_km == pytest.approx(2.0)

    def test_round_trip_proven_by_the_odometer_alone(self):
        v = _vehicle()
        t = _base()
        _r(v, t, odometer=100.0)
        _r(v, t + 10 * M, ignition=False, dlat=0.00005, odometer=101.1)  # back where it started, +1.1 km
        assert len(_trips(v)) == 1

    def test_odometer_rounding_step_while_parked_is_no_trip(self):
        v = _vehicle()
        t = _base()
        for i in range(60):
            _r(v, t + i * 5 * S, odometer=108605.0 if i < 40 else 108605.1)
        _r(v, t + 300 * S, ignition=False, odometer=108605.1)
        assert _trips(v) == []

    def test_odometer_counter_reset_or_set_is_not_movement(self):
        v = _vehicle()
        t = _base()
        _r(v, t, odometer=70.4)
        _r(v, t + 30 * S, odometer=108605.0)  # device odometer set to the vehicle's real reading
        _r(v, t + 60 * S, ignition=False, odometer=108605.0)
        assert _trips(v) == []

    def test_a_single_gps_spike_is_not_movement(self):
        v = _vehicle()
        t = _base()
        _r(v, t, odometer=10.0)
        _r(v, t + 10 * S, dlat=0.05, odometer=10.0)  # one fix 5.5 km away, impossibly fast
        _r(v, t + 20 * S, odometer=10.0)
        _r(v, t + 30 * S, ignition=False, odometer=10.0)
        assert _trips(v) == []

    def test_without_an_odometer_gps_displacement_must_hold_on_two_readings(self):
        once = _vehicle()
        t = _base()
        _r(once, t)
        _r(once, t + 60 * S, dlat=10 * STEP)
        _r(once, t + 120 * S, ignition=False)  # back again: not sustained
        assert _trips(once) == []
        twice = _vehicle()
        _r(twice, t)
        _r(twice, t + 60 * S, dlat=10 * STEP)
        _r(twice, t + 120 * S, ignition=False, dlat=12 * STEP)
        assert len(_trips(twice)) == 1

    def test_no_fix_readings_never_prove_displacement(self):
        v = _vehicle()
        t = _base()
        _r(v, t, lat=0, lon=0, odometer=5.0)
        _r(v, t + 30 * S, metadata={"gps_status": "0"}, dlat=0.01, odometer=5.0)  # device says no fix
        _r(v, t + 60 * S, ignition=False, lat=0, lon=0, odometer=5.0)
        assert _trips(v) == []


class TestEndLogicAndActiveTripsArePreserved:
    def test_short_ignition_off_continues_and_long_off_splits(self):
        v = _vehicle(trip_closure_minutes=5)
        t = _base()
        _session(v, t, [20, 30, 30], dlats=[0, 3 * STEP, 6 * STEP], odometers=[1.0, 1.1, 1.2], off=True)
        # 3 min off -> same trip; then 10 min off -> a new candidate that also moves.
        _session(v, t + 30 * S + 3 * M, [20, 30, 30], dlats=[9 * STEP, 12 * STEP, 15 * STEP], odometers=[1.3, 1.4, 1.5])
        _session(v, t + 30 * S + 3 * M + 30 * S + 10 * M, [20, 30, 30],
                 dlats=[18 * STEP, 21 * STEP, 24 * STEP], odometers=[1.6, 1.7, 1.8])
        trips = _trips(v)
        assert len(trips) == 2
        assert trips[0].end_at == t + 30 * S + 3 * M + 30 * S  # the OFF that the closure rule confirmed

    def test_parked_with_ignition_on_right_now_is_not_an_active_trip_until_it_moves(self):
        v = _vehicle()
        t = timezone.now() - 5 * M
        for i in range(10):
            _r(v, t + i * 10 * S)
        assert _trips(v, now=timezone.now()) == []
        _session(v, t + 2 * M, [20, 25, 30], dlats=[2 * STEP, 5 * STEP, 8 * STEP], off=False)
        trips = _trips(v, now=timezone.now())
        assert len(trips) == 1 and trips[0].status == "ACTIVE" and trips[0].start_at == t + 2 * M


class TestPerVehicleSettings:
    def test_settings_are_per_vehicle(self):
        default, strict = _vehicle(), _vehicle(trip_min_moving_records=5, trip_validation_records=5)
        for v in (default, strict):  # odometer not yet advanced: only the speed rule (A) can confirm
            _session(v, _base(), [0, 8, 10, 9, 0, 0], dlats=[0, 2 * STEP, 4 * STEP, 6 * STEP, 6 * STEP, 6 * STEP],
                     odometers=[1.0] * 6)
        assert len(_trips(default)) == 1  # 3 of 5 moving
        assert _trips(strict) == []  # needs 5 of 5

    def test_minimum_speed_and_distance_are_honoured(self):
        fast_only = _vehicle(trip_min_speed_kmh=20, trip_min_distance_m=500)
        _session(fast_only, _base(), [0, 8, 10, 9, 12], dlats=[0, 2 * STEP, 4 * STEP, 6 * STEP, 8 * STEP])
        assert _trips(fast_only) == []

    def test_defaults_for_existing_and_new_vehicles(self):
        v = VehicleFactory()
        assert (v.trip_validation_records, v.trip_min_moving_records, v.trip_min_speed_kmh, v.trip_min_distance_m) == (
            5, 3, 5, 50)


class TestVehicleScreen:
    def _manager(self, client):
        client.force_login(UserFactory(role=role_with(
            (Permission.Module.VEHICLE, Permission.Action.VIEW), (Permission.Module.VEHICLE, Permission.Action.UPDATE),
        )))

    def _payload(self, vehicle, **overrides):
        data = {
            "registration_number": vehicle.registration_number, "vehicle_code": vehicle.vehicle_code,
            "vehicle_type": vehicle.vehicle_type_id, "fuel_type": vehicle.fuel_type_id, "make": vehicle.make,
            "model": vehicle.model, "status": vehicle.status, "availability_status": vehicle.availability_status,
            "odometer_reading": str(vehicle.odometer_reading), "odometer_unit": vehicle.odometer_unit,
            "ownership_type": vehicle.ownership_type,
        }
        data.update(overrides)
        return data

    def test_edit_screen_shows_and_saves_the_settings(self, client):
        v = VehicleFactory()
        self._manager(client)
        html = client.get(reverse("vehicles:vehicle_edit", kwargs={"uuid": v.uuid})).content.decode()
        for name in ("trip_validation_records", "trip_min_moving_records", "trip_min_speed_kmh", "trip_min_distance_m"):
            assert f'name="{name}"' in html
        response = client.post(reverse("vehicles:vehicle_edit", kwargs={"uuid": v.uuid}), self._payload(
            v, trip_closure_minutes="5", trip_validation_records="8", trip_min_moving_records="4",
            trip_min_speed_kmh="7", trip_min_distance_m="120"))
        assert response.status_code == 302
        v.refresh_from_db()
        assert (v.trip_validation_records, v.trip_min_moving_records, v.trip_min_speed_kmh, v.trip_min_distance_m) == (
            8, 4, 7, 120)

    def test_more_moving_records_than_validation_records_is_rejected(self, client):
        v = VehicleFactory()
        self._manager(client)
        response = client.post(reverse("vehicles:vehicle_edit", kwargs={"uuid": v.uuid}), self._payload(
            v, trip_closure_minutes="5", trip_validation_records="4", trip_min_moving_records="6",
            trip_min_speed_kmh="5", trip_min_distance_m="50"))
        assert response.status_code == 200
        assert "trip_min_moving_records" in response.context["form"].errors
        assert Vehicle.objects.get(pk=v.pk).trip_min_moving_records == 3

    def test_older_clients_that_omit_the_fields_keep_the_values(self, client):
        v = VehicleFactory(trip_min_distance_m=200)
        self._manager(client)
        response = client.post(reverse("vehicles:vehicle_edit", kwargs={"uuid": v.uuid}), self._payload(v, make="X"))
        assert response.status_code == 302
        v.refresh_from_db()
        assert v.make == "X" and v.trip_min_distance_m == 200
