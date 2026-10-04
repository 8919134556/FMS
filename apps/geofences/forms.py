import json
import math
from decimal import Decimal

from django import forms

from apps.core.forms import BootstrapFormMixin
from apps.core.scoping import scope_queryset
from apps.geofences.models import MAX_SPEED_LIMIT_KMH, Geofence
from apps.vehicles.models import Vehicle

GEOFENCE_FIELDS = [
    "code", "name", "description", "geofence_type", "speed_limit_kmh", "vehicles",
    "shape", "center_latitude", "center_longitude", "radius_meters",
    "site", "branch", "status",
]

OPTIONAL_GEOFENCE_FIELDS = ["description", "site", "branch", "vehicles", "speed_limit_kmh"]
MAX_POLYGON_POINTS = 200
MIN_RADIUS_M, MAX_RADIUS_M = 50, 50000


def _distance_m(lat1, lon1, lat2, lon2):
    from apps.geofences.services import distance_meters

    return distance_meters(lat1, lon1, lat2, lon2)


class GeofenceForm(BootstrapFormMixin, forms.ModelForm):
    # Polygon vertices as JSON ([[lat, lon], ...]), written by the map editor.
    polygon_points = forms.CharField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = Geofence
        fields = GEOFENCE_FIELDS
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
            "speed_limit_kmh": forms.NumberInput(attrs={"min": 1, "max": MAX_SPEED_LIMIT_KMH, "step": 1}),
            "vehicles": forms.CheckboxSelectMultiple,
            "geofence_type": forms.RadioSelect,
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        for name in OPTIONAL_GEOFENCE_FIELDS:
            self.fields[name].required = False
        # Radio buttons, not a text-like control (the mixin styles every widget as form-control).
        self.fields["geofence_type"].widget.attrs["class"] = "form-check-input"
        # Assignable vehicles: only those the user can see (client scoping), GPS-tracked first.
        self.fields["vehicles"].queryset = scope_queryset(Vehicle.objects.all(), user).order_by("registration_number")
        self.fields["vehicles"].label_from_instance = lambda v: v.registration_number
        # A polygon's center/radius are derived from its vertices, so they are not typed in.
        for name in ("center_latitude", "center_longitude", "radius_meters"):
            self.fields[name].required = False
        if self.instance.pk and self.instance.shape == Geofence.Shape.POLYGON:
            self.initial["polygon_points"] = json.dumps(self.instance.polygon)

    # -- type / speed limit --
    def clean_speed_limit_kmh(self):
        value = self.cleaned_data.get("speed_limit_kmh")
        if self.cleaned_data.get("geofence_type") != Geofence.GeofenceType.SPEED_LIMIT:
            return None  # only Speed Limit geofences carry a limit
        if value is None:
            raise forms.ValidationError("Enter the speed limit for a Speed Limit geofence.")
        if value <= 0:
            raise forms.ValidationError("The speed limit must be greater than 0 km/h.")
        if value > MAX_SPEED_LIMIT_KMH:
            raise forms.ValidationError(f"The speed limit must be {MAX_SPEED_LIMIT_KMH} km/h or less.")
        return value

    # -- circle --
    def clean_center_latitude(self):
        value = self.cleaned_data.get("center_latitude")
        if value is not None and not (-90 <= value <= 90):
            raise forms.ValidationError("Latitude must be between -90 and 90.")
        return value

    def clean_center_longitude(self):
        value = self.cleaned_data.get("center_longitude")
        if value is not None and not (-180 <= value <= 180):
            raise forms.ValidationError("Longitude must be between -180 and 180.")
        return value

    def clean_radius_meters(self):
        value = self.cleaned_data.get("radius_meters")
        if value is None:
            return value
        if value < MIN_RADIUS_M:
            raise forms.ValidationError("Radius must be at least 50 meters.")
        if value > MAX_RADIUS_M:
            raise forms.ValidationError("Radius must be 50,000 meters or less.")
        return value

    # -- polygon --
    def _parse_polygon(self, raw):
        try:
            points = json.loads(raw or "[]")
        except (TypeError, ValueError):
            raise forms.ValidationError("The polygon could not be read. Draw it again on the map.") from None
        if not isinstance(points, list) or len(points) < 3:
            raise forms.ValidationError("Draw a polygon with at least 3 points on the map.")
        if len(points) > MAX_POLYGON_POINTS:
            raise forms.ValidationError(f"A polygon can have at most {MAX_POLYGON_POINTS} points.")
        clean = []
        for point in points:
            try:
                lat, lon = float(point[0]), float(point[1])
            except (TypeError, ValueError, IndexError):
                raise forms.ValidationError("The polygon has an invalid point. Draw it again on the map.") from None
            if not (-90 <= lat <= 90 and -180 <= lon <= 180) or not (math.isfinite(lat) and math.isfinite(lon)):
                raise forms.ValidationError("The polygon has a point outside valid coordinates.")
            clean.append([round(lat, 6), round(lon, 6)])
        return clean

    def clean(self):
        cleaned = super().clean()
        shape = cleaned.get("shape")
        if shape == Geofence.Shape.POLYGON:
            try:
                points = self._parse_polygon(cleaned.get("polygon_points"))
            except forms.ValidationError as error:
                self.add_error(None, error)
                return cleaned
            # Enclosing circle: vertex centroid + farthest vertex (used for the quick pre-check).
            lat = sum(p[0] for p in points) / len(points)
            lon = sum(p[1] for p in points) / len(points)
            radius = max(_distance_m(lat, lon, p[0], p[1]) for p in points)
            if radius < 10:
                self.add_error(None, "The polygon is too small. Draw a larger area.")
                return cleaned
            if radius > MAX_RADIUS_M * 4:
                self.add_error(None, "The polygon is too large (it must fit within 200 km).")
                return cleaned
            cleaned["polygon"] = points
            cleaned["center_latitude"] = Decimal(f"{lat:.6f}")
            cleaned["center_longitude"] = Decimal(f"{lon:.6f}")
            cleaned["radius_meters"] = max(1, math.ceil(radius))
        elif shape == Geofence.Shape.CIRCLE:
            for name, label in (("center_latitude", "Center latitude"), ("center_longitude", "Center longitude"),
                                ("radius_meters", "Radius")):
                if cleaned.get(name) is None and name not in self.errors:
                    self.add_error(name, f"{label} is required for a circle (click the map to place it).")
            cleaned["polygon"] = []
        return cleaned

    def save(self, commit=True, actor=None):
        geofence = super().save(commit=False)
        geofence.polygon = self.cleaned_data.get("polygon", [])
        geofence.center_latitude = self.cleaned_data["center_latitude"]
        geofence.center_longitude = self.cleaned_data["center_longitude"]
        geofence.radius_meters = self.cleaned_data["radius_meters"]
        if not geofence.pk:
            geofence.created_by = actor
        geofence.updated_by = actor
        if commit:
            geofence.save()
            self.save_m2m()
        return geofence
