from django.db import models

from apps.core.models import BaseFleetModel


class Branch(BaseFleetModel):
    """Branch / depot master — minimal pass, FK target for Vehicle.branch.

    Client Sites (per-client delivery/pickup locations) are a separate
    model — see Site below — that belongs to the dedicated Sites module.
    """

    class BranchType(models.TextChoices):
        DEPOT = "DEPOT", "Depot"
        OFFICE = "OFFICE", "Office"
        YARD = "YARD", "Yard"
        WORKSHOP = "WORKSHOP", "Workshop"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=150)
    branch_type = models.CharField(max_length=15, choices=BranchType.choices, default=BranchType.DEPOT)
    client = models.ForeignKey(
        "clients.Client", null=True, blank=True, on_delete=models.SET_NULL, related_name="branches"
    )
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)
    pincode = models.CharField(max_length=10, blank=True)
    manager = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="managed_branches"
    )
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.ACTIVE, db_index=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["code"]), models.Index(fields=["status"])]

    def __str__(self):
        return self.name


class Site(BaseFleetModel):
    """Client site master — a per-client pickup/delivery/operating location.

    Distinct from Branch: a Branch is Zentora's own depot/office/yard; a
    Site is a location that belongs to a client's operation (a warehouse,
    store, or customer address the fleet serves). Trip origin/destination
    become real FKs to this model once the Trips module ships.
    """

    class SiteType(models.TextChoices):
        WAREHOUSE = "WAREHOUSE", "Warehouse"
        DISTRIBUTION_CENTER = "DISTRIBUTION_CENTER", "Distribution Center"
        RETAIL_OUTLET = "RETAIL_OUTLET", "Retail Outlet"
        FACTORY = "FACTORY", "Factory"
        OFFICE = "OFFICE", "Office"
        CUSTOMER_LOCATION = "CUSTOMER_LOCATION", "Customer Location"
        OTHER = "OTHER", "Other"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"

    site_code = models.CharField(max_length=20, unique=True)
    site_name = models.CharField(max_length=150)
    site_type = models.CharField(max_length=20, choices=SiteType.choices, default=SiteType.WAREHOUSE)
    client = models.ForeignKey("clients.Client", on_delete=models.CASCADE, related_name="sites")
    branch = models.ForeignKey(
        "locations.Branch", null=True, blank=True, on_delete=models.SET_NULL, related_name="sites"
    )

    contact_name = models.CharField(max_length=120, blank=True)
    contact_phone = models.CharField(max_length=20, blank=True)
    contact_email = models.EmailField(blank=True)

    address_line_1 = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)
    pincode = models.CharField(max_length=10, blank=True)

    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    operating_hours = models.CharField(max_length=150, blank=True, help_text="e.g. Mon-Sat 09:00-18:00")
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["site_name"]
        indexes = [models.Index(fields=["site_code"]), models.Index(fields=["status"])]
        verbose_name = "Client Site"

    def __str__(self):
        return f"{self.site_name} ({self.client.client_name})"
