from django import forms

from apps.core.forms import BootstrapFormMixin
from apps.drivers.models import Driver
from apps.vehicles.models import Vehicle, VehicleDriverAssignment

DRIVER_FIELDS = [
    # Personal
    "employee_id", "first_name", "middle_name", "last_name", "date_of_birth", "gender",
    "profile_photo", "mobile_number", "alternate_mobile_number", "email",
    "address", "city", "state", "country", "pincode",
    # Employment
    "driver_type", "employment_type", "vendor", "client", "branch", "date_of_joining", "employment_status",
    # License
    "license_number", "license_type", "license_issue_date", "license_expiry_date",
    "issuing_authority", "issuing_state",
    # Professional
    "experience_years", "emergency_contact_name", "emergency_contact_number", "preferred_shift",
]

OPTIONAL_DRIVER_FIELDS = [
    "middle_name", "date_of_birth", "gender", "profile_photo", "alternate_mobile_number", "email",
    "address", "city", "state", "country", "pincode", "vendor", "client", "branch", "date_of_joining",
    "license_type", "license_issue_date", "license_expiry_date", "issuing_authority", "issuing_state",
    "experience_years", "emergency_contact_name", "emergency_contact_number", "preferred_shift",
]


class DriverForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Driver
        fields = DRIVER_FIELDS
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}),
            "date_of_joining": forms.DateInput(attrs={"type": "date"}),
            "license_issue_date": forms.DateInput(attrs={"type": "date"}),
            "license_expiry_date": forms.DateInput(attrs={"type": "date"}),
            "address": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in OPTIONAL_DRIVER_FIELDS:
            self.fields[name].required = False

    def save(self, commit=True, actor=None):
        driver = super().save(commit=False)
        if not driver.pk:
            driver.created_by = actor
        driver.updated_by = actor
        if commit:
            driver.save()
        return driver


class _VehicleChoiceField(forms.ModelChoiceField):
    """Labels each option with its current driver, if any, so a fleet manager
    sees a reassignment conflict before submitting instead of discovering it
    only after (assign_driver ends the previous assignment automatically —
    see apps.vehicles.services — this label is what makes that visible)."""

    def label_from_instance(self, vehicle):
        base = f"{vehicle.registration_number} — {vehicle.make} {vehicle.model}"
        if vehicle.current_driver_id:
            return f"{base} (currently assigned to {vehicle.current_driver.get_full_name()})"
        return base


class DriverAssignVehicleForm(BootstrapFormMixin, forms.Form):
    vehicle = _VehicleChoiceField(queryset=Vehicle.objects.none(), label="Vehicle")
    assignment_type = forms.ChoiceField(
        choices=VehicleDriverAssignment.AssignmentType.choices, initial=VehicleDriverAssignment.AssignmentType.PRIMARY
    )
    primary_driver = forms.BooleanField(required=False, initial=True, label="Set as primary driver")
    start_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    remarks = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vehicle"].queryset = Vehicle.objects.filter(
            status=Vehicle.Status.ACTIVE
        ).select_related("current_driver").order_by("registration_number")
