import uuid

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models

from apps.core.models import TimeStampedModel, UUIDModel
from apps.core.validators import (
    mobile_number_validator,
    pincode_validator,
    validate_file_size,
    validate_image_extension,
)


class Permission(UUIDModel, TimeStampedModel):
    """A single grantable (module, action) pair. Seeded by ``seed_roles``.

    Modules cover every planned FMS domain, not just the ones implemented so
    far — permission rows are metadata (plain strings), so defining
    ``vehicle.create`` today costs nothing and means Phase 3 doesn't need a
    data migration just to grant it to a role.
    """

    class Module(models.TextChoices):
        USER = "user", "User"
        ROLE = "role", "Role"
        CLIENT = "client", "Client"
        SITE = "site", "Client Site"
        VENDOR = "vendor", "Vendor"
        BRANCH = "branch", "Branch / Depot"
        CONTRACT = "contract", "Contract"
        VEHICLE = "vehicle", "Vehicle"
        VEHICLE_TYPE = "vehicle_type", "Vehicle Type"
        DRIVER = "driver", "Driver"
        ASSIGNMENT = "assignment", "Vehicle-Driver Assignment"
        DOCUMENT = "document", "Document"
        INSURANCE = "insurance", "Insurance"
        TRACKING_DEVICE = "tracking_device", "GPS Device"
        MAINTENANCE = "maintenance", "Maintenance"
        ROUTE = "route", "Route"
        TRIP = "trip", "Trip"
        GEOFENCE = "geofence", "Geofence"
        ALERT = "alert", "Alert"
        NOTIFICATION = "notification", "Notification"
        MASTER_DATA = "master_data", "Master Data"
        AUDIT_LOG = "audit_log", "Audit Log"
        SETTINGS = "settings", "Settings"

    class Action(models.TextChoices):
        VIEW = "view", "View"
        CREATE = "create", "Create"
        UPDATE = "update", "Update"
        DELETE = "delete", "Delete"
        ARCHIVE = "archive", "Archive"
        APPROVE = "approve", "Approve"
        EXPORT = "export", "Export"
        COMMAND = "command", "Send Command"

    module = models.CharField(max_length=30, choices=Module.choices)
    action = models.CharField(max_length=20, choices=Action.choices)
    code = models.CharField(max_length=60, unique=True, editable=False)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["module", "action"]
        constraints = [
            models.UniqueConstraint(fields=["module", "action"], name="uniq_permission_module_action"),
        ]

    def __str__(self):
        return self.code

    def save(self, *args, **kwargs):
        self.code = f"{self.module}.{self.action}"
        super().save(*args, **kwargs)


class Role(UUIDModel, TimeStampedModel):
    name = models.CharField(max_length=100, unique=True)
    code = models.SlugField(max_length=100, unique=True)
    description = models.CharField(max_length=255, blank=True)
    is_system_role = models.BooleanField(
        default=False,
        help_text="System roles are seeded by the platform and cannot be renamed or deleted.",
    )
    is_active = models.BooleanField(default=True)
    permissions = models.ManyToManyField(Permission, through="RolePermission", related_name="roles", blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class RolePermission(models.Model):
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="role_permissions")
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE, related_name="permission_roles")
    assigned_at = models.DateTimeField(auto_now_add=True)
    assigned_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["role", "permission"], name="uniq_role_permission"),
        ]

    def __str__(self):
        return f"{self.role.code}:{self.permission.code}"


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, username, email, password, **extra_fields):
        if not username:
            raise ValueError("Users must have a username.")
        if not email:
            raise ValueError("Users must have an email address.")
        email = self.normalize_email(email)
        user = self.model(username=username, email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(username, email, password, **extra_fields)

    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("status", User.Status.ACTIVE)
        extra_fields.setdefault("employee_id", f"SUPER-{uuid.uuid4().hex[:8].upper()}")
        extra_fields.setdefault("first_name", "System")
        extra_fields.setdefault("last_name", "Administrator")
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(username, email, password, **extra_fields)

    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)


def profile_photo_upload_path(instance, filename):
    return f"profile_photos/{instance.uuid}/{filename}"


class User(AbstractBaseUser, PermissionsMixin, UUIDModel, TimeStampedModel):
    """Custom user model for FMS staff/administrators and client users.

    ``client`` is the tenant boundary. A user with ``client`` set is a
    *client user*: every list, detail, API, report and dashboard the app
    serves them is restricted to that one client's records, and they are
    read-only (see apps.core.scoping). A user with ``client`` empty is
    internal staff and is governed by role permissions alone.

    PROTECT (not SET_NULL) on purpose: deleting a client must never turn its
    users into unscoped staff.
    """

    class Gender(models.TextChoices):
        MALE = "MALE", "Male"
        FEMALE = "FEMALE", "Female"
        OTHER = "OTHER", "Other"
        UNDISCLOSED = "UNDISCLOSED", "Prefer not to say"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"
        SUSPENDED = "SUSPENDED", "Suspended"
        LOCKED = "LOCKED", "Locked"
        INVITED = "INVITED", "Invited"

    employee_id = models.CharField(max_length=20, unique=True)
    username = models.CharField(max_length=150, unique=True)
    first_name = models.CharField(max_length=100)
    middle_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    mobile_number = models.CharField(max_length=16, unique=True, validators=[mobile_number_validator])
    alternate_mobile_number = models.CharField(max_length=16, blank=True, validators=[mobile_number_validator])
    profile_photo = models.ImageField(
        upload_to=profile_photo_upload_path,
        null=True,
        blank=True,
        validators=[validate_file_size, validate_image_extension],
    )
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=15, choices=Gender.choices, blank=True)
    designation = models.CharField(max_length=100, blank=True)
    department = models.CharField(max_length=100, blank=True)
    role = models.ForeignKey(Role, null=True, blank=True, on_delete=models.PROTECT, related_name="users")
    client = models.ForeignKey(
        "clients.Client", null=True, blank=True, on_delete=models.PROTECT, related_name="users",
        help_text="Set for client users: they only ever see this client's data, read-only. Leave empty for internal staff.",
    )
    reporting_manager = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="direct_reports"
    )
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)
    pincode = models.CharField(max_length=10, blank=True, validators=[pincode_validator])
    date_of_joining = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.ACTIVE, db_index=True)

    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    created_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    objects = UserManager()
    all_objects = BaseUserManager()

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = ["email", "employee_id", "first_name", "last_name", "mobile_number"]

    class Meta:
        ordering = ["first_name", "last_name"]
        indexes = [
            models.Index(fields=["employee_id"]),
            models.Index(fields=["status"]),
            models.Index(fields=["email"]),
        ]

    def __str__(self):
        return f"{self.get_full_name()} ({self.username})"

    def get_full_name(self):
        parts = [self.first_name, self.middle_name, self.last_name]
        return " ".join(p for p in parts if p).strip() or self.username

    def get_short_name(self):
        return self.first_name or self.username

    def save(self, *args, **kwargs):
        self.is_active = self.status not in {self.Status.INACTIVE, self.Status.SUSPENDED, self.Status.LOCKED}
        super().save(*args, **kwargs)

    def delete(self, using=None, keep_parents=False, deleted_by=None):
        from django.utils import timezone

        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.deleted_by = deleted_by
        self.status = self.Status.INACTIVE
        self.save(using=using, update_fields=["is_deleted", "deleted_at", "deleted_by", "status", "is_active"])

    def restore(self):
        self.is_deleted = False
        self.deleted_at = None
        self.deleted_by = None
        self.status = self.Status.ACTIVE
        self.save(update_fields=["is_deleted", "deleted_at", "deleted_by", "status", "is_active"])
