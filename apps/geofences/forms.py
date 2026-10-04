from django import forms

from apps.core.forms import BootstrapFormMixin
from apps.geofences.models import Geofence

GEOFENCE_FIELDS = [
    "code", "name", "description", "center_latitude", "center_longitude", "radius_meters",
    "site", "branch", "notify_on_enter", "notify_on_exit", "status",
]

OPTIONAL_GEOFENCE_FIELDS = ["description", "site", "branch"]


class GeofenceForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Geofence
        fields = GEOFENCE_FIELDS
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in OPTIONAL_GEOFENCE_FIELDS:
            self.fields[name].required = False

    def clean_center_latitude(self):
        value = self.cleaned_data["center_latitude"]
        if not (-90 <= value <= 90):
            raise forms.ValidationError("Latitude must be between -90 and 90.")
        return value

    def clean_center_longitude(self):
        value = self.cleaned_data["center_longitude"]
        if not (-180 <= value <= 180):
            raise forms.ValidationError("Longitude must be between -180 and 180.")
        return value

    def clean_radius_meters(self):
        value = self.cleaned_data["radius_meters"]
        if value < 50:
            raise forms.ValidationError("Radius must be at least 50 meters.")
        if value > 50000:
            raise forms.ValidationError("Radius must be 50,000 meters or less.")
        return value

    def save(self, commit=True, actor=None):
        geofence = super().save(commit=False)
        if not geofence.pk:
            geofence.created_by = actor
        geofence.updated_by = actor
        if commit:
            geofence.save()
        return geofence
