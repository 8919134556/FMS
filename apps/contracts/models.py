from django.db import models

from apps.core.models import BaseFleetModel


class Contract(BaseFleetModel):
    """Contract master — minimal pass, FK target for Vehicle.contract."""

    class ContractType(models.TextChoices):
        LEASE = "LEASE", "Lease"
        SERVICE = "SERVICE", "Service"
        SUPPLY = "SUPPLY", "Supply"
        MAINTENANCE = "MAINTENANCE", "Maintenance"
        OTHER = "OTHER", "Other"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        ACTIVE = "ACTIVE", "Active"
        EXPIRING = "EXPIRING", "Expiring"
        EXPIRED = "EXPIRED", "Expired"
        TERMINATED = "TERMINATED", "Terminated"
        RENEWED = "RENEWED", "Renewed"

    contract_number = models.CharField(max_length=30, unique=True)
    client = models.ForeignKey(
        "clients.Client", null=True, blank=True, on_delete=models.SET_NULL, related_name="contracts"
    )
    vendor = models.ForeignKey(
        "vendors.Vendor", null=True, blank=True, on_delete=models.SET_NULL, related_name="contracts"
    )
    contract_type = models.CharField(max_length=15, choices=ContractType.choices, default=ContractType.LEASE)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    contract_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.DRAFT, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["contract_number"]), models.Index(fields=["status"])]

    def __str__(self):
        return self.contract_number
