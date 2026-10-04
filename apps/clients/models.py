from django.db import models

from apps.core.models import BaseFleetModel


class Client(BaseFleetModel):
    """Client master.

    Deliberately minimal in this pass — Vehicles needs a real FK target for
    ownership/assignment today. The full Client experience (sites, contracts,
    contacts, billing, activity tabs) is a dedicated module; this model gains
    those extra fields additively when that module is built, rather than
    being modeled twice.
    """

    class ClientType(models.TextChoices):
        CORPORATE = "CORPORATE", "Corporate"
        GOVERNMENT = "GOVERNMENT", "Government"
        SME = "SME", "SME"
        INDIVIDUAL = "INDIVIDUAL", "Individual"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"
        SUSPENDED = "SUSPENDED", "Suspended"
        PROSPECT = "PROSPECT", "Prospect"
        CLOSED = "CLOSED", "Closed"

    client_code = models.CharField(max_length=20, unique=True)
    client_name = models.CharField(max_length=150)
    legal_name = models.CharField(max_length=180, blank=True)
    client_type = models.CharField(max_length=20, choices=ClientType.choices, default=ClientType.CORPORATE)
    industry = models.CharField(max_length=100, blank=True)

    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)

    address_line_1 = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)
    pincode = models.CharField(max_length=10, blank=True)

    account_manager = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="managed_clients"
    )
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["client_name"]
        indexes = [models.Index(fields=["client_code"]), models.Index(fields=["status"])]

    def __str__(self):
        return self.client_name
