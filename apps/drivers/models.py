from django.db import models

from apps.core.models import BaseFleetModel
from apps.core.validators import mobile_number_validator, pincode_validator, validate_file_size, validate_image_extension


def driver_photo_upload_path(instance, filename):
    return f"driver_photos/{instance.uuid}/{filename}"


class Driver(BaseFleetModel):
    """Driver master.

    Covers Personal/Employment/License/Operations fields now, since Vehicle
    assignment needs a real driver to assign against. The full Driver module
    (documents, trip history, performance scoring) builds on this same model.
    """

    class Gender(models.TextChoices):
        MALE = "MALE", "Male"
        FEMALE = "FEMALE", "Female"
        OTHER = "OTHER", "Other"
        UNDISCLOSED = "UNDISCLOSED", "Prefer not to say"

    class DriverType(models.TextChoices):
        COMPANY = "COMPANY", "Company Driver"
        CONTRACT = "CONTRACT", "Contract Driver"
        OWNER_OPERATOR = "OWNER_OPERATOR", "Owner-Operator"

    class EmploymentType(models.TextChoices):
        FULL_TIME = "FULL_TIME", "Full-Time"
        PART_TIME = "PART_TIME", "Part-Time"
        CONTRACT = "CONTRACT", "Contract"

    class EmploymentStatus(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"
        SUSPENDED = "SUSPENDED", "Suspended"
        ON_LEAVE = "ON_LEAVE", "On Leave"
        TERMINATED = "TERMINATED", "Terminated"

    class Shift(models.TextChoices):
        DAY = "DAY", "Day"
        NIGHT = "NIGHT", "Night"
        ROTATING = "ROTATING", "Rotating"

    # Single source of truth for "expiring soon" — reused by the model
    # property below, list/detail filtering, and every template that shows
    # a license badge, so the threshold only ever lives in one place.
    LICENSE_EXPIRY_WARNING_DAYS = 30

    # Personal
    employee_id = models.CharField(max_length=20, unique=True)
    first_name = models.CharField(max_length=100)
    middle_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100)
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=15, choices=Gender.choices, blank=True)
    profile_photo = models.ImageField(
        upload_to=driver_photo_upload_path, null=True, blank=True,
        validators=[validate_file_size, validate_image_extension],
    )
    mobile_number = models.CharField(max_length=16, unique=True, validators=[mobile_number_validator])
    alternate_mobile_number = models.CharField(max_length=16, blank=True, validators=[mobile_number_validator])
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)
    pincode = models.CharField(max_length=10, blank=True, validators=[pincode_validator])

    # Employment
    driver_type = models.CharField(max_length=20, choices=DriverType.choices, default=DriverType.COMPANY)
    employment_type = models.CharField(max_length=15, choices=EmploymentType.choices, default=EmploymentType.FULL_TIME)
    vendor = models.ForeignKey("vendors.Vendor", null=True, blank=True, on_delete=models.SET_NULL, related_name="drivers")
    client = models.ForeignKey("clients.Client", null=True, blank=True, on_delete=models.SET_NULL, related_name="drivers")
    branch = models.ForeignKey("locations.Branch", null=True, blank=True, on_delete=models.SET_NULL, related_name="drivers")
    date_of_joining = models.DateField(null=True, blank=True)
    employment_status = models.CharField(
        max_length=15, choices=EmploymentStatus.choices, default=EmploymentStatus.ACTIVE, db_index=True
    )

    # License
    license_number = models.CharField(max_length=40, unique=True)
    license_type = models.CharField(max_length=60, blank=True)
    license_issue_date = models.DateField(null=True, blank=True)
    license_expiry_date = models.DateField(null=True, blank=True)
    issuing_authority = models.CharField(max_length=120, blank=True)
    issuing_state = models.CharField(max_length=100, blank=True)

    # Professional
    experience_years = models.PositiveSmallIntegerField(null=True, blank=True)
    emergency_contact_name = models.CharField(max_length=120, blank=True)
    emergency_contact_number = models.CharField(max_length=16, blank=True)
    preferred_shift = models.CharField(max_length=10, choices=Shift.choices, blank=True)

    class Meta:
        ordering = ["first_name", "last_name"]
        indexes = [
            models.Index(fields=["employee_id"]),
            models.Index(fields=["license_number"]),
            models.Index(fields=["employment_status"]),
        ]

    def __str__(self):
        return f"{self.get_full_name()} ({self.employee_id})"

    def get_full_name(self):
        parts = [self.first_name, self.middle_name, self.last_name]
        return " ".join(p for p in parts if p).strip()

    @property
    def license_expiry_status(self):
        """Used by templates to badge the license as valid/expiring/expired
        without duplicating this date logic in every template."""
        if not self.license_expiry_date:
            return None
        from django.utils import timezone

        days_left = (self.license_expiry_date - timezone.now().date()).days
        if days_left < 0:
            return "EXPIRED"
        if days_left <= self.LICENSE_EXPIRY_WARNING_DAYS:
            return "EXPIRING_SOON"
        return "VALID"
