import factory
from factory.django import DjangoModelFactory

from apps.contracts.models import Contract


class ContractFactory(DjangoModelFactory):
    class Meta:
        model = Contract

    contract_number = factory.Sequence(lambda n: f"CTR-{n:06d}")
    contract_type = Contract.ContractType.LEASE
    status = Contract.Status.ACTIVE
