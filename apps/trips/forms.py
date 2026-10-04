from django import forms
from django.db.models import Q

from apps.core.forms import BootstrapFormMixin
from apps.drivers.models import Driver
from apps.locations.models import Site
from apps.routes.models import Route
from apps.trips.models import Trip
from apps.vehicles.models import Vehicle


class ClientScopedSelect(forms.Select):
    """Stamps each <option> with data-client so trip-form.js can hard-filter
    the Site pickers to the selected client (and soft-prefer Vehicle/Driver
    options for that client) without an extra query per option — the map is
    built once from the already-fetched queryset, not looked up per row."""

    def __init__(self, *args, client_map=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.client_map = client_map or {}

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        raw_value = value.value if hasattr(value, "value") else value
        client_id = self.client_map.get(str(raw_value)) if raw_value not in (None, "") else None
        if client_id is not None:
            option["attrs"]["data-client"] = client_id
        return option


TRIP_FIELDS = [
    "trip_type", "priority", "client", "origin_site", "destination_site",
    "vehicle", "driver", "route", "scheduled_start", "scheduled_end",
    "planned_distance", "instructions", "internal_notes",
]

OPTIONAL_TRIP_FIELDS = ["vehicle", "driver", "route", "planned_distance", "instructions", "internal_notes"]


class TripForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Trip
        fields = TRIP_FIELDS
        widgets = {
            "scheduled_start": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "scheduled_end": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "instructions": forms.Textarea(attrs={"rows": 3}),
            "internal_notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in OPTIONAL_TRIP_FIELDS:
            self.fields[name].required = False
        self.fields["scheduled_start"].input_formats = ["%Y-%m-%dT%H:%M"]
        self.fields["scheduled_end"].input_formats = ["%Y-%m-%dT%H:%M"]

        site_qs = Site.objects.filter(status=Site.Status.ACTIVE).select_related("client")
        vehicle_qs = Vehicle.objects.filter(status=Vehicle.Status.ACTIVE).select_related("client")
        driver_qs = Driver.objects.filter(employment_status=Driver.EmploymentStatus.ACTIVE).select_related("client")
        self.fields["origin_site"].queryset = site_qs
        self.fields["destination_site"].queryset = site_qs
        self.fields["vehicle"].queryset = vehicle_qs
        self.fields["driver"].queryset = driver_qs
        self.fields["route"].queryset = Route.objects.filter(status=Route.Status.ACTIVE)

        # Every site/vehicle/driver ships to the browser with a data-client
        # attribute (see trip-form.js) so the pickers can be narrowed to the
        # selected client client-side; the server still re-validates the
        # relationship in Trip.clean() regardless of what the UI sent.
        site_client_map = {str(s.pk): str(s.client_id) for s in site_qs}
        vehicle_client_map = {str(v.pk): str(v.client_id) for v in vehicle_qs if v.client_id}
        driver_client_map = {str(d.pk): str(d.client_id) for d in driver_qs if d.client_id}
        for field_name, client_map in [
            ("origin_site", site_client_map), ("destination_site", site_client_map),
            ("vehicle", vehicle_client_map), ("driver", driver_client_map),
        ]:
            field = self.fields[field_name]
            field.widget = ClientScopedSelect(attrs=field.widget.attrs, client_map=client_map)

    def save(self, commit=True, actor=None):
        trip = super().save(commit=False)
        if not trip.pk:
            trip.created_by = actor
        trip.updated_by = actor
        if commit:
            trip.save()
        return trip


class TripAssignForm(BootstrapFormMixin, forms.Form):
    vehicle = forms.ModelChoiceField(queryset=Vehicle.objects.none(), label="Vehicle")
    driver = forms.ModelChoiceField(queryset=Driver.objects.none(), label="Driver")

    def __init__(self, *args, trip=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.trip = trip
        vehicle_qs = Vehicle.objects.filter(status=Vehicle.Status.ACTIVE).select_related("client")
        driver_qs = Driver.objects.filter(employment_status=Driver.EmploymentStatus.ACTIVE).select_related("client")
        if trip is not None:
            # Restrict to the trip's own client's fleet plus unallocated
            # (pool) vehicles/drivers, which are valid for any client —
            # matches the same "belongs to a different client -> blocked"
            # rule apps.trips.services.assign_trip enforces server-side.
            vehicle_qs = vehicle_qs.filter(
                Q(client_id=trip.client_id) | Q(client_id__isnull=True)
            ).order_by("registration_number")
            driver_qs = driver_qs.filter(
                Q(client_id=trip.client_id) | Q(client_id__isnull=True)
            ).order_by("first_name", "last_name")
        self.fields["vehicle"].queryset = vehicle_qs
        self.fields["driver"].queryset = driver_qs


class TripDelayForm(BootstrapFormMixin, forms.Form):
    delay_reason = forms.ChoiceField(choices=Trip.DelayReason.choices, label="Delay Reason")
    delay_notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}), label="Notes")


class TripCompleteForm(BootstrapFormMixin, forms.Form):
    actual_end = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        input_formats=["%Y-%m-%dT%H:%M"], label="Actual End Time",
    )
    actual_distance = forms.DecimalField(required=False, max_digits=8, decimal_places=2, label="Actual Distance (km)")
    fuel_used = forms.DecimalField(required=False, max_digits=7, decimal_places=2, label="Fuel Used (litres)")
    driver_remarks = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}), label="Driver Remarks")
    completion_notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}), label="Notes")


class TripCancelForm(BootstrapFormMixin, forms.Form):
    cancellation_reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), label="Cancellation Reason")
