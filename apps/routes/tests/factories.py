import factory
from factory.django import DjangoModelFactory

from apps.locations.tests.factories import SiteFactory
from apps.routes.models import Route, RouteStop


class RouteFactory(DjangoModelFactory):
    class Meta:
        model = Route

    code = factory.Sequence(lambda n: f"RT{n:05d}")
    name = factory.Sequence(lambda n: f"Route {n}")
    status = Route.Status.ACTIVE


class RouteStopFactory(DjangoModelFactory):
    class Meta:
        model = RouteStop

    route = factory.SubFactory(RouteFactory)
    site = factory.SubFactory(SiteFactory)
    sequence = factory.Sequence(lambda n: n)
