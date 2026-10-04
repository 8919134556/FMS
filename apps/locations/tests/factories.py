import factory
from factory.django import DjangoModelFactory

from apps.clients.tests.factories import ClientFactory
from apps.locations.models import Branch, Site


class BranchFactory(DjangoModelFactory):
    class Meta:
        model = Branch

    code = factory.Sequence(lambda n: f"BR{n:05d}")
    name = factory.Sequence(lambda n: f"Branch {n}")
    branch_type = Branch.BranchType.DEPOT
    status = Branch.Status.ACTIVE


class SiteFactory(DjangoModelFactory):
    class Meta:
        model = Site

    site_code = factory.Sequence(lambda n: f"SITE{n:05d}")
    site_name = factory.Sequence(lambda n: f"Site {n}")
    site_type = Site.SiteType.WAREHOUSE
    client = factory.SubFactory(ClientFactory)
    status = Site.Status.ACTIVE
