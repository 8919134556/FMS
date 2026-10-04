from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Q
from django.http import JsonResponse
from django.views.generic import View

from apps.core.permissions import user_has_permission
from apps.core.scoping import client_scope_id
from apps.geofences.models import Geofence


class GeofenceMapDataView(LoginRequiredMixin, View):
    """Feeds the Live Tracking map's geofence layer — static boundary data
    (shape, type, limit), never telemetry.

    Who may load it: anyone who can open Live Tracking (``tracking_device``
    view) or the Geofences module (``geofence`` view). What they get: only
    ACTIVE geofences (an inactive geofence is disabled — not monitored, not
    drawn); a client user only those assigned to at least one of their own
    client's vehicles, with only those vehicles counted — another client's
    zones and vehicles are never revealed. Staff / superusers get all.

    Polled by the page on its own slow cycle (not with the vehicle feed);
    the page redraws only when the returned data actually changed."""

    def get(self, request, *args, **kwargs):
        user = request.user
        if not (user_has_permission(user, "tracking_device", "view") or user_has_permission(user, "geofence", "view")):
            return JsonResponse({"detail": "You don't have access to geofences."}, status=403)

        geofences = Geofence.objects.filter(status=Geofence.Status.ACTIVE)
        client_id = client_scope_id(user)
        if client_id is not None:
            geofences = geofences.filter(
                pk__in=Geofence.objects.filter(vehicles__client_id=client_id).values("pk"))
            vehicle_count = Count("vehicles", filter=Q(vehicles__client_id=client_id), distinct=True)
        else:
            vehicle_count = Count("vehicles", distinct=True)
        geofences = geofences.annotate(vehicle_count=vehicle_count).only(
            "uuid", "name", "center_latitude", "center_longitude", "radius_meters", "shape", "polygon",
            "geofence_type", "speed_limit_kmh", "status",
        ).order_by("name")
        results = [
            {
                "uuid": str(g.uuid),
                "name": g.name,
                "latitude": str(g.center_latitude),
                "longitude": str(g.center_longitude),
                "radius_meters": g.radius_meters,
                "shape": g.shape,
                "polygon": g.polygon if g.shape == Geofence.Shape.POLYGON else [],
                "type": g.geofence_type,
                "type_label": g.type_summary,
                "type_name": g.get_geofence_type_display(),
                "speed_limit_kmh": g.speed_limit_kmh,
                "status": g.get_status_display(),
                "assigned_vehicles": g.vehicle_count,
            }
            for g in geofences
        ]
        return JsonResponse({"results": results})
