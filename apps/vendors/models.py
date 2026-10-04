from django.db import models

from apps.core.models import BaseFleetModel


class Vendor(BaseFleetModel):
    """Vendor / fleet owner master — minimal pass, FK target for Vehicle.vendor."""

    class VendorType(models.TextChoices):
        VEHICLE_OWNER = "VEHICLE_OWNER", "Vehicle Owner"
        DRIVER_SUPPLIER = "DRIVER_SUPPLIER", "Driver Supplier"
        MAINTENANCE = "MAINTENANCE", "Maintenance Vendor"
        GPS = "GPS", "GPS Vendor"
        FUEL = "FUEL", "Fuel Vendor"
        OTHER = "OTHER", "Other"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"

    vendor_code = models.CharField(max_length=20, unique=True)
    vendor_name = models.CharField(max_length=150)
    vendor_type = models.CharField(max_length=20, choices=VendorType.choices, default=VendorType.VEHICLE_OWNER)
    contact_person = models.CharField(max_length=120, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)
    pincode = models.CharField(max_length=10, blank=True)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.ACTIVE, db_index=True)

    class Meta:
        ordering = ["vendor_name"]
        indexes = [models.Index(fields=["vendor_code"]), models.Index(fields=["status"])]

    def __str__(self):
        return self.vendor_name
