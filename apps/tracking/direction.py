"""Vehicle direction for Live Tracking (fleet feed ``direction``).

Source, in order:

1. the device's own heading (``heading``) — only when the device really reports
   one: some recent reading has a non-zero value. (The comms App DB currently
   stores ``direction = 0`` for every reading, so a constant 0 is treated as
   "no heading", not as "north".)
2. otherwise the bearing between the vehicle's consecutive GPS positions: from
   the last position at least ``MIN_MOVE_METRES`` away to the current one —
   short GPS jitter around a parked vehicle never counts as movement.

Stationary vehicles keep the direction of their last movement (``moving``
False); with no usable movement in the window the direction is unknown.
"""

import math
from datetime import timedelta

from django.db.models import Q

from apps.tracking.models import TelemetryEvent

WINDOW = timedelta(minutes=30)  # how far back to look for the previous position
MIN_MOVE_METRES = 25.0  # below this, a position change is GPS noise, not movement
MAX_POINTS = 60  # per vehicle

COMPASS = [
    ("N", "North"), ("NE", "North-East"), ("E", "East"), ("SE", "South-East"),
    ("S", "South"), ("SW", "South-West"), ("W", "West"), ("NW", "North-West"),
]


def _fix(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if (lat == 0 and lon == 0) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None  # (0,0) = the device's "no GPS fix yet"
    return lat, lon


def distance_m(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def bearing(a, b):
    """Initial bearing from a to b, degrees clockwise from north (0-359)."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    y = math.sin(lon2 - lon1) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def compass(degrees):
    code, label = COMPASS[int(((degrees % 360) + 22.5) // 45) % 8]
    return code, label


def _result(degrees, source, moving):
    if degrees is None:
        return {"degrees": None, "cardinal": None, "label": None, "source": None, "moving": moving}
    degrees = int(round(degrees)) % 360
    code, label = compass(degrees)
    return {"degrees": degrees, "cardinal": code, "label": label, "source": source, "moving": moving}


def direction_for(current, history, *, moving):
    """``current``: the VehicleCurrentTelemetry row; ``history``: its recent readings, newest first,
    as (timestamp, lat, lon, heading) tuples."""
    here = _fix(current.latitude, current.longitude)
    device_reports_heading = any(h not in (None, 0) for _, _, _, h in history) or current.heading not in (None, 0)
    if moving and device_reports_heading and current.heading is not None:
        return _result(current.heading, "device", moving)
    if here is None:
        return _result(None, None, moving)
    for _, lat, lon, _ in history:
        point = _fix(lat, lon)
        if point and distance_m(point, here) >= MIN_MOVE_METRES:
            return _result(bearing(point, here), "gps", moving)
    return _result(None, None, moving)


def directions_by_vehicle(rows, moving_by_vehicle):
    """vehicle_id -> direction dict for these VehicleCurrentTelemetry rows, in ONE query (each
    vehicle's readings in the WINDOW before its own latest reading, so offline vehicles keep
    their last direction)."""
    rows = [r for r in rows if r.timestamp is not None]
    if not rows:
        return {}
    window = Q()
    for row in rows:
        window |= Q(vehicle_id=row.vehicle_id, timestamp__gte=row.timestamp - WINDOW, timestamp__lte=row.timestamp)
    history = {}
    for vehicle_id, ts, lat, lon, heading in (
        TelemetryEvent.objects.filter(window)
        .order_by("vehicle_id", "-timestamp")
        .values_list("vehicle_id", "timestamp", "latitude", "longitude", "heading")
    ):
        points = history.setdefault(vehicle_id, [])
        if len(points) < MAX_POINTS:
            points.append((ts, lat, lon, heading))
    return {
        row.vehicle_id: direction_for(row, history.get(row.vehicle_id, []), moving=moving_by_vehicle.get(row.vehicle_id, False))
        for row in rows
    }
