import datetime

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.clients.tests.factories import ClientFactory
from apps.drivers.tests.factories import DriverFactory
from apps.locations.tests.factories import SiteFactory
from apps.trips import services
from apps.trips.models import Trip
from apps.trips.tests.factories import TripFactory
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


class TestTripNumberGeneration:
    def test_generates_sequential_numbers(self):
        first = services.generate_trip_number()
        second = services.generate_trip_number()
        assert first != second
        assert first.startswith("TRP-")
        assert int(second.split("-")[1]) == int(first.split("-")[1]) + 1

    def test_trip_number_is_unique(self):
        TripFactory(trip_number="TRP-999999")
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                TripFactory(trip_number="TRP-999999")


class TestTripValidation:
    def test_scheduled_end_before_start_rejected(self):
        client = ClientFactory()
        origin = SiteFactory(client=client)
        destination = SiteFactory(client=client)
        trip = Trip(
            trip_number="TRP-100001", client=client, origin_site=origin, destination_site=destination,
            scheduled_start=timezone.now() + datetime.timedelta(hours=5),
            scheduled_end=timezone.now() + datetime.timedelta(hours=1),
        )
        with pytest.raises(ValidationError):
            trip.full_clean()

    def test_same_origin_and_destination_rejected(self):
        client = ClientFactory()
        site = SiteFactory(client=client)
        trip = Trip(
            trip_number="TRP-100002", client=client, origin_site=site, destination_site=site,
            scheduled_start=timezone.now() + datetime.timedelta(hours=1),
            scheduled_end=timezone.now() + datetime.timedelta(hours=5),
        )
        with pytest.raises(ValidationError):
            trip.full_clean()

    def test_origin_site_from_different_client_rejected(self):
        client_a = ClientFactory()
        client_b = ClientFactory()
        origin = SiteFactory(client=client_b)
        destination = SiteFactory(client=client_a)
        trip = Trip(
            trip_number="TRP-100003", client=client_a, origin_site=origin, destination_site=destination,
            scheduled_start=timezone.now() + datetime.timedelta(hours=1),
            scheduled_end=timezone.now() + datetime.timedelta(hours=5),
        )
        with pytest.raises(ValidationError):
            trip.full_clean()

    def test_destination_site_from_different_client_rejected(self):
        client_a = ClientFactory()
        client_b = ClientFactory()
        origin = SiteFactory(client=client_a)
        destination = SiteFactory(client=client_b)
        trip = Trip(
            trip_number="TRP-100004", client=client_a, origin_site=origin, destination_site=destination,
            scheduled_start=timezone.now() + datetime.timedelta(hours=1),
            scheduled_end=timezone.now() + datetime.timedelta(hours=5),
        )
        with pytest.raises(ValidationError):
            trip.full_clean()

    def test_vehicle_from_different_client_rejected(self):
        client_a = ClientFactory()
        client_b = ClientFactory()
        vehicle = VehicleFactory(client=client_b)
        trip = TripFactory.build(client=client_a, vehicle=vehicle)
        trip.origin_site = SiteFactory(client=client_a)
        trip.destination_site = SiteFactory(client=client_a)
        with pytest.raises(ValidationError):
            trip.full_clean()

    def test_driver_from_different_client_rejected(self):
        client_a = ClientFactory()
        client_b = ClientFactory()
        driver = DriverFactory(client=client_b)
        trip = TripFactory.build(client=client_a, driver=driver)
        trip.origin_site = SiteFactory(client=client_a)
        trip.destination_site = SiteFactory(client=client_a)
        with pytest.raises(ValidationError):
            trip.full_clean()

    def test_unallocated_vehicle_allowed_for_any_client(self):
        client = ClientFactory()
        vehicle = VehicleFactory(client=None)
        trip = TripFactory.build(client=client, vehicle=vehicle)
        trip.origin_site = SiteFactory(client=client)
        trip.destination_site = SiteFactory(client=client)
        trip.full_clean()  # should not raise


class TestTripStatusTransitions:
    def test_valid_transition_allowed(self):
        trip = TripFactory(status=Trip.Status.DRAFT)
        assert trip.can_transition_to(Trip.Status.SCHEDULED) is True

    def test_invalid_transition_rejected(self):
        trip = TripFactory(status=Trip.Status.COMPLETED)
        assert trip.can_transition_to(Trip.Status.IN_PROGRESS) is False

    def test_cancelled_cannot_go_to_scheduled(self):
        trip = TripFactory(status=Trip.Status.CANCELLED)
        assert trip.can_transition_to(Trip.Status.SCHEDULED) is False

    def test_completed_is_terminal(self):
        trip = TripFactory(status=Trip.Status.COMPLETED)
        assert trip.VALID_TRANSITIONS[Trip.Status.COMPLETED] == set()

    def test_cancelled_is_terminal(self):
        trip = TripFactory(status=Trip.Status.CANCELLED)
        assert trip.VALID_TRANSITIONS[Trip.Status.CANCELLED] == set()

    def test_delayed_can_return_to_in_progress(self):
        trip = TripFactory(status=Trip.Status.DELAYED)
        assert trip.can_transition_to(Trip.Status.IN_PROGRESS) is True


class TestTripProperties:
    def test_route_label(self):
        origin = SiteFactory(site_name="Warehouse A")
        destination = SiteFactory(site_name="Store B")
        trip = TripFactory(origin_site=origin, destination_site=destination)
        assert trip.route_label == "Warehouse A → Store B"

    def test_estimated_duration(self):
        start = timezone.now() + datetime.timedelta(hours=1)
        end = start + datetime.timedelta(hours=4, minutes=30)
        trip = TripFactory(scheduled_start=start, scheduled_end=end)
        assert trip.estimated_duration == datetime.timedelta(hours=4, minutes=30)

    def test_is_on_time_none_when_not_completed(self):
        trip = TripFactory(status=Trip.Status.IN_PROGRESS)
        assert trip.is_on_time is None
