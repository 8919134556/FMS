from django.http import JsonResponse
from django.views.generic import View

from apps.core.permissions import ModulePermissionRequiredMixin
from apps.geofences.models import Geofence


class GeofenceMapDataView(ModulePermissionRequiredMixin, View):
    """Feeds the Live Tracking map's geofence overlay — static boundary
    reference data (center/radius), not live telemetry, so it's fine to
    return in full rather than through the no-server-side-telemetry API."""

    permission_module = "geofence"
    permission_action = "view"

    def get(self, request, *args, **kwargs):
        geofences = Geofence.objects.filter(status=Geofence.Status.ACTIVE).only(
            "uuid", "name", "center_latitude", "center_longitude", "radius_meters"
        )
        results = [
            {
                "uuid": str(g.uuid),
                "name": g.name,
                "latitude": str(g.center_latitude),
                "longitude": str(g.center_longitude),
                "radius_meters": g.radius_meters,
            }
            for g in geofences
        ]
        return JsonResponse({"results": results})
