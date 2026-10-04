import datetime

import factory
from django.utils import timezone
from factory.django import DjangoModelFactory

from apps.clients.tests.factories import ClientFactory
from apps.locations.tests.factories import SiteFactory
from apps.trips.models import Trip


class TripFactory(DjangoModelFactory):
    class Meta:
        model = Trip

    trip_number = factory.Sequence(lambda n: f"TRP-{n:06d}")
    client = factory.SubFactory(ClientFactory)
    trip_type = Trip.TripType.DELIVERY
    priority = Trip.Priority.NORMAL
    status = Trip.Status.SCHEDULED
    scheduled_start = factory.LazyFunction(lambda: timezone.now() + datetime.timedelta(hours=1))
    scheduled_end = factory.LazyFunction(lambda: timezone.now() + datetime.timedelta(hours=5))

    @factory.lazy_attribute
    def origin_site(self):
        return SiteFactory(client=self.client)

    @factory.lazy_attribute
    def destination_site(self):
        return SiteFactory(client=self.client)
