from django import forms
from django.forms import inlineformset_factory

from apps.core.forms import BootstrapFormMixin
from apps.maintenance.models import Maintenance, MaintenancePart
from apps.vehicles.models import Vehicle

MAINTENANCE_FIELDS = [
    "vehicle", "maintenance_type", "priority",
    "scheduled_date", "expected_completion_date",
    "odometer_at_service", "next_service_odometer", "next_service_date",
    "service_center", "technician", "description", "notes",
    "estimated_cost",
]

OPTIONAL_MAINTENANCE_FIELDS = [
    "expected_completion_date", "next_service_odometer", "next_service_date",
    "service_center", "technician", "description", "notes", "estimated_cost",
]

# A vehicle that's been taken out of service permanently shouldn't be
# schedulable for maintenance — matches the "vehicle is not deactivated"
# create-time validation the spec calls for.
NOT_SCHEDULABLE_VEHICLE_STATUSES = [
    Vehicle.Status.INACTIVE, Vehicle.Status.SOLD, Vehicle.Status.RETIRED, Vehicle.Status.SCRAPPED,
]


class MaintenanceForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Maintenance
        fields = MAINTENANCE_FIELDS
        widgets = {
            "scheduled_date": forms.DateInput(attrs={"type": "date"}),
            "expected_completion_date": forms.DateInput(attrs={"type": "date"}),
            "next_service_date": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in OPTIONAL_MAINTENANCE_FIELDS:
            self.fields[name].required = False
        self.fields["vehicle"].queryset = Vehicle.objects.exclude(
            status__in=NOT_SCHEDULABLE_VEHICLE_STATUSES
        ).select_related("vehicle_type").order_by("registration_number")

    def save(self, commit=True, actor=None):
        maintenance = super().save(commit=False)
        if not maintenance.pk:
            maintenance.created_by = actor
        maintenance.updated_by = actor
        if commit:
            maintenance.save()
        return maintenance


class MaintenancePartForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = MaintenancePart
        fields = ["part_name", "part_number", "quantity", "unit_cost"]


MaintenancePartFormSet = inlineformset_factory(
    Maintenance, MaintenancePart, form=MaintenancePartForm, extra=3, can_delete=True,
)


class MaintenanceCompleteForm(BootstrapFormMixin, forms.Form):
    completed_date = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        input_formats=["%Y-%m-%dT%H:%M"], label="Completed Date",
    )
    final_odometer = forms.DecimalField(max_digits=10, decimal_places=1, label="Final Odometer", required=False)
    actual_cost = forms.DecimalField(max_digits=12, decimal_places=2, required=False, label="Actual Cost")
    labor_cost = forms.DecimalField(max_digits=12, decimal_places=2, required=False, label="Labor Cost")
    work_performed = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}), label="Work Performed")
    completion_notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}), label="Notes")
    next_service_date = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"type": "date"}), label="Next Service Date",
    )
    next_service_odometer = forms.DecimalField(
        required=False, max_digits=10, decimal_places=1, label="Next Service Odometer",
    )


class MaintenanceCancelForm(BootstrapFormMixin, forms.Form):
    cancellation_reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), label="Cancellation Reason")
