from django import forms

from apps.core.forms import BootstrapFormMixin
from apps.drivers.models import Driver
from apps.vehicles.models import (
    MAX_TRIP_CLOSURE_MINUTES,
    Vehicle,
    VehicleDriverAssignment,
    VehicleType,
)

# Per-vehicle trip-detection settings (Vehicle.trip_* — see
# apps.tracking.trip_report): closure time + movement validation.
TRIP_SETTING_FIELDS = (
    "trip_closure_minutes", "trip_validation_records", "trip_min_moving_records",
    "trip_min_speed_kmh", "trip_min_distance_m", "idle_alert_minutes",
)

VEHICLE_FIELDS = [
    # 1. Identification
    "registration_number", "vehicle_code", "vin", "chassis_number", "engine_number",
    # 2. Vehicle information
    "vehicle_type", "category", "manufacturer", "make", "model", "variant", "model_year",
    "manufacturing_date", "color",
    # 3. Technical
    "fuel_type", "transmission_type", "engine_capacity", "seating_capacity", "load_capacity", "mileage",
    # 4. Ownership
    "ownership_type", "owner_name", "vendor", "client", "contract", "branch",
    # 5. Operational
    "status", "availability_status", "odometer_reading", "odometer_unit", *TRIP_SETTING_FIELDS,
    # 6. Financial
    "purchase_date", "purchase_price", "current_value",
    # Other
    "image", "remarks",
]

OPTIONAL_VEHICLE_FIELDS = [
    "vin", "chassis_number", "engine_number", "category", "manufacturer", "variant", "model_year",
    "manufacturing_date", "color", "transmission_type", "engine_capacity", "seating_capacity",
    "load_capacity", "mileage", "owner_name", "vendor", "client", "contract", "branch",
    "purchase_date", "purchase_price", "current_value", "image", "remarks",
]


class VehicleForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Vehicle
        fields = VEHICLE_FIELDS
        widgets = {
            "manufacturing_date": forms.DateInput(attrs={"type": "date"}),
            "purchase_date": forms.DateInput(attrs={"type": "date"}),
            "remarks": forms.Textarea(attrs={"rows": 3}),
            **{name: forms.NumberInput(attrs={"step": 1, "inputmode": "numeric"}) for name in TRIP_SETTING_FIELDS},
        }
        error_messages = {
            "trip_closure_minutes": {
                "required": "Enter the trip closure time in minutes.",
                "invalid": "Trip closure time must be a whole number of minutes.",
                "min_value": "Trip closure time must be at least 1 minute.",
                "max_value": f"Trip closure time can be at most {MAX_TRIP_CLOSURE_MINUTES} minutes (24 hours).",
            },
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in OPTIONAL_VEHICLE_FIELDS:
            self.fields[name].required = False
        self.fields["vehicle_type"].queryset = self.fields["vehicle_type"].queryset.filter(is_active=True)
        self.fields["fuel_type"].queryset = self.fields["fuel_type"].queryset.filter(is_active=True)
        self.fields["category"].queryset = self.fields["category"].queryset.filter(is_active=True)
        for name in TRIP_SETTING_FIELDS:
            field = self.fields[name]
            # A PositiveSmallIntegerField renders min="0"; give the browser's own
            # check the same limits as the model's validators.
            limits = {type(v).__name__: v.limit_value for v in Vehicle._meta.get_field(name).validators}
            field.min_value = limits.get("MinValueValidator", field.min_value)
            field.widget.attrs.update({"min": limits.get("MinValueValidator"), "max": limits.get("MaxValueValidator")})
            # The Vehicle screen always submits these, so a blank value there is a
            # validation error. A request that predates a field and omits it
            # entirely (an older script/integration posting this form) must not
            # start failing: it keeps the vehicle's current value, or the default
            # for a new vehicle — see clean().
            if self.is_bound and name not in self.data:
                field.required = False

    def clean(self):
        cleaned = super().clean()
        for name in TRIP_SETTING_FIELDS:
            if cleaned.get(name) is None and name not in self.data:  # only when omitted (see __init__)
                cleaned[name] = (getattr(self.instance, name) if self.instance.pk
                                 else Vehicle._meta.get_field(name).get_default())
        return cleaned

    def save(self, commit=True, actor=None):
        vehicle = super().save(commit=False)
        if not vehicle.pk:
            vehicle.created_by = actor
        vehicle.updated_by = actor
        if commit:
            vehicle.save()
        return vehicle


class VehicleAssignDriverForm(BootstrapFormMixin, forms.Form):
    driver = forms.ModelChoiceField(queryset=Driver.objects.none(), label="Driver")
    assignment_type = forms.ChoiceField(
        choices=VehicleDriverAssignment.AssignmentType.choices, initial=VehicleDriverAssignment.AssignmentType.PRIMARY
    )
    primary_driver = forms.BooleanField(required=False, initial=True, label="Set as primary driver")
    start_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    remarks = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["driver"].queryset = Driver.objects.filter(
            employment_status=Driver.EmploymentStatus.ACTIVE
        ).order_by("first_name", "last_name")


VEHICLE_TYPE_FIELDS = ["code", "name", "category", "description", "is_active"]
OPTIONAL_VEHICLE_TYPE_FIELDS = ["category", "description"]


class VehicleTypeForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = VehicleType
        fields = VEHICLE_TYPE_FIELDS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in OPTIONAL_VEHICLE_TYPE_FIELDS:
            self.fields[name].required = False
        self.fields["category"].queryset = self.fields["category"].queryset.filter(is_active=True)
