import factory
from factory.django import DjangoModelFactory

from apps.clients.models import Client


class ClientFactory(DjangoModelFactory):
    class Meta:
        model = Client

    client_code = factory.Sequence(lambda n: f"CLI{n:05d}")
    client_name = factory.Sequence(lambda n: f"Client {n}")
    client_type = Client.ClientType.CORPORATE
    status = Client.Status.ACTIVE
