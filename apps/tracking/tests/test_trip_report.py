"""Ignition-derived Trip Report: the ON/OFF merge rule, active-trip handling,
distance, day-wise bucketing and client scoping — all computed from the
existing TelemetryEvent history, nothing persisted."""

import datetime

import pytest
from django.utils import timezone

from apps.clients.tests.factories import ClientFactory
from apps.tracking import trip_report
from apps.tracking.tests.factories import TrackingDeviceFactory, TelemetryEventFactory
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db

GRACE = datetime.timedelta(minutes=5)


def _vehicle(client_obj=None, **kw):
    vehicle = VehicleFactory(client=client_obj, **kw)
    TrackingDeviceFactory(vehicle=vehicle)
    return vehicle


def _event(vehicle, client_obj, when, ignition, odometer=None, lat="12.971600", lon="77.594600", location="",
           moving=True):
    """One reading. An ignition-ON reading here stands for a real departure, so
    by default it carries speed and is followed by two readings ~55 m apart
    (5 s, 10 s later) — enough for movement confirmation (3 of 5 readings at
    speed, and away from the start), with the trip starting at THIS reading.
    ``moving=False`` gives a bare ignition-ON (a candidate that never moves)."""
    event = TelemetryEventFactory(
        device=vehicle.tracking_device,
        vehicle=vehicle,
        client=client_obj,
        timestamp=when,
        ignition=ignition,
        odometer=odometer,
        latitude=lat,
        longitude=lon,
        speed=30 if (ignition and moving) else 0,
        metadata={"location": location} if location else {},
    )
    if ignition and moving:
        for step in (1, 2):
            TelemetryEventFactory(
                device=vehicle.tracking_device, vehicle=vehicle, client=client_obj,
                timestamp=when + datetime.timedelta(seconds=5 * step), ignition=True, odometer=odometer,
                latitude=f"{float(lat) + 0.0005 * step:.6f}" if float(lat) else "12.971600",
                longitude=lon if float(lat) else "77.594600", speed=30, metadata={},
            )
    return event


def _compute(user, now, **kw):
    return trip_report.compute_vehicle_trips(user=user, now=now, **kw)


class TestBasicDetection:
    def test_ignition_on_then_off_past_grace_is_one_completed_trip(self):
        client_obj = ClientFactory()
        vehicle = _vehicle(client_obj)
        base = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
        _event(vehicle, client_obj, base, True, odometer=100)
        _event(vehicle, client_obj, base + datetime.timedelta(minutes=30), False, odometer=115)
        now = base + datetime.timedelta(hours=2)

        trips, summary = _compute(None, now, start_date=base.date(), end_date=base.date())

        assert len(trips) == 1
        trip = trips[0]
        assert trip.status == "COMPLETED"
        assert trip.start_at == base
        assert trip.end_at == base + datetime.timedelta(minutes=30)
        assert trip.distance_km == 15
        assert summary["total_trips"] == 1 and summary["active_trips"] == 0

    def test_short_ignition_off_within_grace_stays_the_same_trip(self):
        client_obj = ClientFactory()
        vehicle = _vehicle(client_obj)
        base = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
        _event(vehicle, client_obj, base, True, odometer=100)
        _event(vehicle, client_obj, base + datetime.timedelta(minutes=10), False, odometer=110)  # a red light
        _event(vehicle, client_obj, base + datetime.timedelta(minutes=12), True, odometer=110)  # 2 min later: same trip
        _event(vehicle, client_obj, base + datetime.timedelta(minutes=40), False, odometer=130)
        now = base + datetime.timedelta(hours=2)

        trips, _ = _compute(None, now, start_date=base.date(), end_date=base.date())

        assert len(trips) == 1
        assert trips[0].start_at == base
        assert trips[0].end_at == base + datetime.timedelta(minutes=40)
        assert trips[0].distance_km == 30

    def test_ignition_off_past_grace_then_back_on_is_two_trips(self):
        client_obj = ClientFactory()
        vehicle = _vehicle(client_obj)
        base = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
        _event(vehicle, client_obj, base, True, odometer=100)
        _event(vehicle, client_obj, base + datetime.timedelta(minutes=10), False, odometer=110)
        _event(vehicle, client_obj, base + datetime.timedelta(minutes=20), True, odometer=110)  # 10 min later: new trip
        _event(vehicle, client_obj, base + datetime.timedelta(minutes=30), False, odometer=118)
        now = base + datetime.timedelta(hours=2)

        trips, summary = _compute(None, now, start_date=base.date(), end_date=base.date())

        assert len(trips) == 2
        assert summary["total_trips"] == 2
        newest, oldest = trips  # sorted newest-first
        assert oldest.start_at == base and oldest.end_at == base + datetime.timedelta(minutes=10)
        assert newest.start_at == base + datetime.timedelta(minutes=20)
        assert newest.end_at == base + datetime.timedelta(minutes=30)

    def test_exactly_at_the_closure_time_closes_the_trip(self):
        """Ignition OFF for the closure time "or more" closes the trip — a gap
        of exactly 5:00 on a 5-minute vehicle starts a new trip."""
        client_obj = ClientFactory()
        vehicle = _vehicle(client_obj)
        base = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
        _event(vehicle, client_obj, base, True, odometer=100)
        _event(vehicle, client_obj, base + datetime.timedelta(minutes=10), False, odometer=105)
        _event(vehicle, client_obj, base + datetime.timedelta(minutes=15), True, odometer=105)  # exactly GRACE later
        now = base + datetime.timedelta(hours=1)

        trips, _ = _compute(None, now, start_date=base.date(), end_date=base.date())
        assert len(trips) == 2
        assert trips[1].status == "COMPLETED" and trips[1].end_at == base + datetime.timedelta(minutes=10)

    def test_just_under_the_closure_time_still_merges(self):
        client_obj = ClientFactory()
        vehicle = _vehicle(client_obj)
        base = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
        _event(vehicle, client_obj, base, True, odometer=100)
        _event(vehicle, client_obj, base + datetime.timedelta(minutes=10), False, odometer=105)
        _event(vehicle, client_obj, base + datetime.timedelta(minutes=14, seconds=59), True, odometer=105)
        now = base + datetime.timedelta(hours=1)

        trips, _ = _compute(None, now, start_date=base.date(), end_date=base.date())
        assert len(trips) == 1  # merged, still driving


class TestActiveTripHandling:
    def test_ignition_currently_on_is_active_with_no_end(self):
        client_obj = ClientFactory()
        vehicle = _vehicle(client_obj)
        base = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
        _event(vehicle, client_obj, base, True, odometer=100)
        now = base + datetime.timedelta(minutes=20)

        trips, summary = _compute(None, now, start_date=base.date(), end_date=base.date())
        assert len(trips) == 1
        assert trips[0].status == "ACTIVE" and trips[0].end_at is None
        assert summary["active_trips"] == 1

    def test_ignition_off_within_grace_is_still_active_not_yet_closed(self):
        client_obj = ClientFactory()
        vehicle = _vehicle(client_obj)
        base = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
        _event(vehicle, client_obj, base, True, odometer=100)
        _event(vehicle, client_obj, base + datetime.timedelta(minutes=10), False, odometer=110)
        now = base + datetime.timedelta(minutes=13)  # only 3 min since ignition off

        trips, _ = _compute(None, now, start_date=base.date(), end_date=base.date())
        assert len(trips) == 1
        assert trips[0].status == "ACTIVE" and trips[0].end_at is None

    def test_ignition_off_within_grace_reports_off_not_on(self):
        """Regression: the trip must reflect the vehicle's real last-known
        ignition state (OFF) while pending closure, not be hardcoded ON just
        because the trip itself is still ACTIVE."""
        client_obj = ClientFactory()
        vehicle = _vehicle(client_obj)
        base = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
        _event(vehicle, client_obj, base, True, odometer=100, location="Depot")
        _event(vehicle, client_obj, base + datetime.timedelta(minutes=10), False, odometer=112, location="Layby")
        now = base + datetime.timedelta(minutes=13)  # 3 min since ignition off — within grace

        trips, _ = _compute(None, now, start_date=base.date(), end_date=base.date())
        trip = trips[0]
        assert trip.status == "ACTIVE"
        assert trip.ignition is False
        # a tentative snapshot of the vehicle's last known (off) position/distance,
        # shown immediately rather than left blank until the trip fully closes
        assert trip.end_location == "Layby"
        assert trip.distance_km == 12

    def test_refreshing_past_the_grace_window_finally_closes_it(self):
        """Same data as above, just queried later — a page refresh must not have
        closed it early, and must close it once the rule is actually satisfied."""
        client_obj = ClientFactory()
        vehicle = _vehicle(client_obj)
        base = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
        _event(vehicle, client_obj, base, True, odometer=100)
        _event(vehicle, client_obj, base + datetime.timedelta(minutes=10), False, odometer=110)

        still_open, _ = _compute(None, base + datetime.timedelta(minutes=14), start_date=base.date(), end_date=base.date())
        assert still_open[0].status == "ACTIVE"

        now_closed, _ = _compute(None, base + datetime.timedelta(minutes=16), start_date=base.date(), end_date=base.date())
        assert now_closed[0].status == "COMPLETED"
        assert now_closed[0].end_at == base + datetime.timedelta(minutes=10)

    def test_active_trip_uses_live_current_telemetry_for_position_and_distance(self):
        from apps.tracking.tests.factories import VehicleCurrentTelemetryFactory

        client_obj = ClientFactory()
        vehicle = _vehicle(client_obj)
        base = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
        _event(vehicle, client_obj, base, True, odometer=100, location="Depot")
        now = base + datetime.timedelta(minutes=30)
        VehicleCurrentTelemetryFactory(
            vehicle=vehicle, device=vehicle.tracking_device, timestamp=now,
            latitude="13.0", longitude="77.6", odometer=140, ignition=True, location="MG Road",
        )

        trips, _ = _compute(None, now, start_date=base.date(), end_date=base.date())
        trip = trips[0]
        assert trip.status == "ACTIVE"
        assert trip.end_location == "MG Road"
        assert str(trip.end_latitude) == "13.000000"
        assert trip.distance_km == 40  # live odometer (140) - trip start odometer (100)
        assert trip.ignition is True

    def test_active_trip_is_shown_even_though_it_started_before_the_selected_range(self):
        client_obj = ClientFactory()
        vehicle = _vehicle(client_obj)
        yesterday_start = timezone.now().replace(hour=23, minute=0, second=0, microsecond=0) - datetime.timedelta(days=1)
        _event(vehicle, client_obj, yesterday_start, True, odometer=100)
        now = yesterday_start + datetime.timedelta(hours=10)  # still driving, now "today"

        trips, _ = _compute(
            None, now, start_date=now.date(), end_date=now.date()  # viewing only "today"
        )
        assert len(trips) == 1
        assert trips[0].status == "ACTIVE"
        assert trips[0].start_at == yesterday_start  # real start, not clipped to today

    def test_a_trip_completed_yesterday_does_not_leak_into_todays_view(self):
        client_obj = ClientFactory()
        vehicle = _vehicle(client_obj)
        yesterday = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0) - datetime.timedelta(days=1)
        _event(vehicle, client_obj, yesterday, True, odometer=100)
        _event(vehicle, client_obj, yesterday + datetime.timedelta(minutes=20), False, odometer=110)
        now = timezone.now()

        trips, _ = _compute(None, now, start_date=now.date(), end_date=now.date())
        assert trips == []


class TestScopingAndFiltering:
    def test_vehicle_without_a_gps_device_is_never_included(self):
        client_obj = ClientFactory()
        VehicleFactory(client=client_obj)  # no TrackingDeviceFactory
        now = timezone.now()
        trips, _ = _compute(None, now, start_date=now.date(), end_date=now.date())
        assert trips == []

    def test_client_user_only_sees_their_own_clients_vehicles(self):
        from apps.accounts.models import Permission
        from apps.accounts.tests.factories import UserFactory
        from apps.core.tests.factories import role_with

        mine, theirs = ClientFactory(), ClientFactory()
        my_vehicle = _vehicle(mine)
        their_vehicle = _vehicle(theirs)
        base = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
        _event(my_vehicle, mine, base, True, odometer=100)
        _event(their_vehicle, theirs, base, True, odometer=200)
        now = base + datetime.timedelta(minutes=5)
        viewer = UserFactory(role=role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW)), client=mine)

        trips, _ = _compute(viewer, now, start_date=base.date(), end_date=base.date())
        assert len(trips) == 1 and trips[0].vehicle == my_vehicle

    def test_vehicle_filter(self):
        client_obj = ClientFactory()
        a, b = _vehicle(client_obj), _vehicle(client_obj)
        base = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
        _event(a, client_obj, base, True, odometer=100)
        _event(b, client_obj, base, True, odometer=200)
        now = base + datetime.timedelta(minutes=5)

        trips, _ = _compute(None, now, start_date=base.date(), end_date=base.date(), vehicle_uuid=str(a.uuid))
        assert len(trips) == 1 and trips[0].vehicle == a

    def test_search_matches_registration_number(self):
        client_obj = ClientFactory()
        a = _vehicle(client_obj, registration_number="KA51EH6363")
        b = _vehicle(client_obj, registration_number="KA01ZZ0001")
        base = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
        _event(a, client_obj, base, True, odometer=100)
        _event(b, client_obj, base, True, odometer=200)
        now = base + datetime.timedelta(minutes=5)

        trips, _ = _compute(None, now, start_date=base.date(), end_date=base.date(), search="6363")
        assert len(trips) == 1 and trips[0].vehicle == a


class TestLocationAndSummary:
    def test_start_location_comes_from_event_metadata(self):
        client_obj = ClientFactory()
        vehicle = _vehicle(client_obj)
        base = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
        _event(vehicle, client_obj, base, True, odometer=100, location="Warehouse A")
        _event(vehicle, client_obj, base + datetime.timedelta(minutes=20), False, odometer=110, location="Client Site B")
        now = base + datetime.timedelta(hours=1)

        trips, _ = _compute(None, now, start_date=base.date(), end_date=base.date())
        assert trips[0].start_location == "Warehouse A"
        assert trips[0].end_location == "Client Site B"

    def test_missing_odometer_leaves_distance_unknown_not_zero(self):
        client_obj = ClientFactory()
        vehicle = _vehicle(client_obj)
        base = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
        _event(vehicle, client_obj, base, True, odometer=None)
        _event(vehicle, client_obj, base + datetime.timedelta(minutes=20), False, odometer=None)
        now = base + datetime.timedelta(hours=1)

        trips, _ = _compute(None, now, start_date=base.date(), end_date=base.date())
        assert trips[0].distance_km is None

    def test_summary_totals(self):
        client_obj = ClientFactory()
        vehicle = _vehicle(client_obj)
        base = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
        _event(vehicle, client_obj, base, True, odometer=100)
        _event(vehicle, client_obj, base + datetime.timedelta(minutes=30), False, odometer=120)
        now = base + datetime.timedelta(hours=1)

        _, summary = _compute(None, now, start_date=base.date(), end_date=base.date())
        assert summary["total_trips"] == 1
        assert summary["total_distance_km"] == 20
        assert summary["avg_duration_seconds"] == 30 * 60


class TestNoGpsFixIsNeverPlottedAsAPosition:
    """Regression: this fleet's real devices report (0, 0) — "Null Island" —
    before they acquire a fix, right as ignition turns on (seen directly in
    production data: gps_status=0 readings at lat=lon=0). That must never be
    shown as a real start/end point on the map or in the Start/End Location
    columns — apps.tracking.trip_report._has_gps_fix."""

    def test_no_fix_reading_is_recognized(self):
        assert trip_report._has_gps_fix(0, 0) is False
        assert trip_report._has_gps_fix(0.0, 0.0) is False
        assert trip_report._has_gps_fix(12.9716, 77.5946) is True
        assert trip_report._has_gps_fix(0, 77.5946) is True  # only one axis zero -> a real (if odd) reading

    def test_start_position_is_blank_not_null_island_when_ignition_turns_on_before_a_fix(self):
        client_obj = ClientFactory()
        vehicle = _vehicle(client_obj)
        base = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
        _event(vehicle, client_obj, base, True, odometer=100, lat="0", lon="0")  # no fix yet
        _event(vehicle, client_obj, base + datetime.timedelta(minutes=30), False, odometer=120)
        now = base + datetime.timedelta(hours=1)

        trips, _ = _compute(None, now, start_date=base.date(), end_date=base.date())
        trip = trips[0]
        assert trip.start_latitude is None and trip.start_longitude is None
        assert trip.start_location == ""
        assert trip.end_latitude is not None  # the end reading has a real fix — unaffected
        assert trip.distance_km == 20  # odometer is independent of GPS fix quality

    def test_end_position_is_blank_when_the_closing_reading_has_no_fix(self):
        client_obj = ClientFactory()
        vehicle = _vehicle(client_obj)
        base = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
        _event(vehicle, client_obj, base, True, odometer=100)
        _event(vehicle, client_obj, base + datetime.timedelta(minutes=30), False, odometer=120, lat="0", lon="0")
        now = base + datetime.timedelta(hours=1)

        trips, _ = _compute(None, now, start_date=base.date(), end_date=base.date())
        trip = trips[0]
        assert trip.start_latitude is not None
        assert trip.end_latitude is None and trip.end_longitude is None and trip.end_location == ""

    def test_a_momentary_no_fix_current_reading_does_not_blank_an_active_trips_last_known_position(self):
        from apps.tracking.tests.factories import VehicleCurrentTelemetryFactory

        client_obj = ClientFactory()
        vehicle = _vehicle(client_obj)
        base = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
        _event(vehicle, client_obj, base, True, odometer=100, location="Depot")
        now = base + datetime.timedelta(minutes=10)
        VehicleCurrentTelemetryFactory(
            vehicle=vehicle, device=vehicle.tracking_device, timestamp=now,
            latitude="0", longitude="0", odometer=110, ignition=True, location="",
        )

        trips, _ = _compute(None, now, start_date=base.date(), end_date=base.date())
        trip = trips[0]
        assert trip.status == "ACTIVE"
        # falls back to the trip's own start position rather than plotting (0, 0)
        assert trip.end_latitude is None  # no reference_off was ever set (still driving) and live update was skipped
        assert trip.start_latitude is not None


class TestDownsample:
    def test_leaves_a_short_list_untouched(self):
        points = list(range(10))
        assert trip_report._downsample(points, 300) == points

    def test_always_keeps_the_first_and_last_point(self):
        points = list(range(1000))
        thinned = trip_report._downsample(points, 50)
        assert thinned[0] == 0 and thinned[-1] == 999
        assert len(thinned) <= 50

    def test_result_stays_in_original_order_with_no_duplicates(self):
        points = list(range(777))
        thinned = trip_report._downsample(points, 100)
        assert thinned == sorted(set(thinned))
