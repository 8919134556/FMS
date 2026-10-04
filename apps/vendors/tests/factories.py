import factory
from factory.django import DjangoModelFactory

from apps.vendors.models import Vendor


class VendorFactory(DjangoModelFactory):
    class Meta:
        model = Vendor

    vendor_code = factory.Sequence(lambda n: f"VND{n:05d}")
    vendor_name = factory.Sequence(lambda n: f"Vendor {n}")
    vendor_type = Vendor.VendorType.VEHICLE_OWNER
    status = Vendor.Status.ACTIVE
