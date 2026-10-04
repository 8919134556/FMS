import pytest
from django.db import IntegrityError, transaction

from apps.locations.tests.factories import SiteFactory

pytestmark = pytest.mark.django_db


class TestSiteUniqueness:
    def test_duplicate_site_code_rejected(self):
        SiteFactory(site_code="DUPSITE01")
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                SiteFactory(site_code="DUPSITE01")

    def test_str_includes_client_name(self):
        site = SiteFactory(site_name="Main Warehouse")
        assert site.client.client_name in str(site)
        assert "Main Warehouse" in str(site)

    def test_site_requires_client(self):
        site = SiteFactory.build(client=None)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                site.save()
