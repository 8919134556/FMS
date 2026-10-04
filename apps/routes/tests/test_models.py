import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.clients.tests.factories import ClientFactory
from apps.locations.tests.factories import SiteFactory
from apps.routes.models import RouteStop
from apps.routes.tests.factories import RouteFactory, RouteStopFactory

pytestmark = pytest.mark.django_db


class TestRoute:
    def test_str_returns_name(self):
        route = RouteFactory(name="Airport Loop")
        assert str(route) == "Airport Loop"

    def test_code_must_be_unique(self):
        RouteFactory(code="DUPCODE")
        with pytest.raises(IntegrityError), transaction.atomic():
            RouteFactory(code="DUPCODE")

    def test_stop_count_reflects_related_stops(self):
        route = RouteFactory()
        RouteStopFactory(route=route, sequence=1)
        RouteStopFactory(route=route, sequence=2)
        assert route.stop_count == 2


class TestRouteStop:
    def test_str_includes_route_and_site(self):
        stop = RouteStopFactory(sequence=1)
        assert stop.route.name in str(stop)

    def test_duplicate_sequence_on_same_route_rejected(self):
        route = RouteFactory()
        RouteStopFactory(route=route, sequence=1)
        with pytest.raises(IntegrityError), transaction.atomic():
            RouteStopFactory(route=route, sequence=1)

    def test_site_must_belong_to_routes_client(self):
        client_a = ClientFactory()
        client_b = ClientFactory()
        route = RouteFactory(client=client_a)
        other_client_site = SiteFactory(client=client_b)
        stop = RouteStop(route=route, site=other_client_site, sequence=1)
        with pytest.raises(ValidationError):
            stop.full_clean()

    def test_site_matching_routes_client_is_valid(self):
        client_a = ClientFactory()
        route = RouteFactory(client=client_a)
        site = SiteFactory(client=client_a)
        stop = RouteStop(route=route, site=site, sequence=1)
        stop.full_clean()  # should not raise
