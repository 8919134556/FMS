import pytest
from django.db import IntegrityError, transaction

from apps.clients.tests.factories import ClientFactory

pytestmark = pytest.mark.django_db


class TestClientUniqueness:
    def test_duplicate_client_code_rejected(self):
        ClientFactory(client_code="DUPCLI01")
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                ClientFactory(client_code="DUPCLI01")

    def test_str_returns_client_name(self):
        client = ClientFactory(client_name="Acme Logistics")
        assert str(client) == "Acme Logistics"
