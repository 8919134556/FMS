"""Geofence breach detection — pure computation, no Celery/scheduler.

``evaluate_position`` is called synchronously right after a vehicle's live
position is written (apps.tracking.services.TelemetryIngestionService.ingest),
so ENTER/EXIT events are recorded as part of the same request that already
persisted the position, not on a lag from a background worker.
"""

import math

from django.utils import timezone

from apps.geofences.models import Geofence, GeofenceEvent
from apps.notifications import services as notification_services

EARTH_RADIUS_METERS = 6371000


def _distance_meters(lat1, lon1, lat2, lon2):
    """Haversine distance — accurate enough for geofence-radius comparisons
    (meters to low kilometers), no external geo library needed."""
    phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2))
    d_phi = math.radians(float(lat2) - float(lat1))
    d_lambda = math.radians(float(lon2) - float(lon1))
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(a))


def evaluate_position(vehicle, latitude, longitude, occurred_at=None):
    """Checks ``vehicle``'s position against every active geofence and
    records an ENTER/EXIT GeofenceEvent on each state transition (never one
    per ping — a vehicle sitting inside a geofence for an hour of pings
    produces exactly one ENTER event, not one per ping). Returns the list of
    events created, mainly for tests."""
    occurred_at = occurred_at or timezone.now()
    created_events = []

    for geofence in Geofence.objects.filter(status=Geofence.Status.ACTIVE):
        distance = _distance_meters(latitude, longitude, geofence.center_latitude, geofence.center_longitude)
        is_inside = distance <= geofence.radius_meters

        last_event = (
            GeofenceEvent.objects.filter(geofence=geofence, vehicle=vehicle).order_by("-occurred_at").first()
        )
        was_inside = last_event is not None and last_event.event_type == GeofenceEvent.EventType.ENTER

        if is_inside and not was_inside:
            event = GeofenceEvent.objects.create(
                geofence=geofence, vehicle=vehicle, event_type=GeofenceEvent.EventType.ENTER,
                latitude=latitude, longitude=longitude, occurred_at=occurred_at,
            )
            created_events.append(event)
            if geofence.notify_on_enter:
                _notify_breach(geofence, vehicle, event)
        elif not is_inside and was_inside:
            event = GeofenceEvent.objects.create(
                geofence=geofence, vehicle=vehicle, event_type=GeofenceEvent.EventType.EXIT,
                latitude=latitude, longitude=longitude, occurred_at=occurred_at,
            )
            created_events.append(event)
            if geofence.notify_on_exit:
                _notify_breach(geofence, vehicle, event)

    return created_events


def _notify_breach(geofence, vehicle, event):
    """Best-effort: notifies the vehicle's client's account manager, if any
    — the same recipient apps.trips.views already notifies for new trips,
    so a client-facing contact learns about zone activity without a new
    concept of "who should know about this vehicle"."""
    if not vehicle.client_id or not vehicle.client.account_manager_id:
        return
    verb = "entered" if event.event_type == GeofenceEvent.EventType.ENTER else "exited"
    notification_services.notify(
        vehicle.client.account_manager,
        title=f"{vehicle.registration_number} {verb} {geofence.name}",
        body=f"At {event.occurred_at:%Y-%m-%d %H:%M}",
        level=notification_services.Notification.Level.INFO,
    )
