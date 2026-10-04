from django import forms
from django.forms import inlineformset_factory

from apps.core.forms import BootstrapFormMixin
from apps.routes.models import Route, RouteStop

ROUTE_FIELDS = ["code", "name", "client", "description", "estimated_distance_km", "estimated_duration_minutes", "status"]
OPTIONAL_ROUTE_FIELDS = ["client", "description", "estimated_distance_km", "estimated_duration_minutes"]


class RouteForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Route
        fields = ROUTE_FIELDS
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in OPTIONAL_ROUTE_FIELDS:
            self.fields[name].required = False

    def save(self, commit=True, actor=None):
        route = super().save(commit=False)
        if not route.pk:
            route.created_by = actor
        route.updated_by = actor
        if commit:
            route.save()
        return route


class RouteStopForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = RouteStop
        fields = ["site", "sequence", "notes"]


RouteStopFormSet = inlineformset_factory(
    Route, RouteStop, form=RouteStopForm, extra=3, can_delete=True, min_num=0, validate_min=False,
)
