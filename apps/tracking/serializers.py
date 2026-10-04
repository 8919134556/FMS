from rest_framework import serializers

from apps.tracking.models import TelemetryEvent, VehicleCurrentTelemetry


class TelemetryCurrentSerializer(serializers.ModelSerializer):
    connection_status = serializers.SerializerMethodField()

    class Meta:
        model = VehicleCurrentTelemetry
        fields = ["latitude", "longitude", "speed", "heading", "ignition", "odometer", "timestamp", "connection_status"]

    def get_connection_status(self, obj):
        return self.context.get("connection_status")


class TelemetryHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = TelemetryEvent
        fields = [
            "timestamp", "latitude", "longitude", "speed", "heading", "altitude",
            "ignition", "odometer", "engine_hours", "battery_voltage",
            "external_power", "signal_strength", "satellite_count",
        ]


class ActiveTripSerializer(serializers.Serializer):
    uuid = serializers.CharField()
    trip_number = serializers.CharField()


class FleetCurrentTelemetrySerializer(serializers.Serializer):
    """Feeds the Live Fleet Map (Phase 3.4) — a plain Serializer, not a
    ModelSerializer, since each item is a pre-merged dict from
    apps.tracking.services.fleet_current_telemetry() (VehicleCurrentTelemetry
    + an active-Trip lookup), not a single model instance."""

    vehicle_uuid = serializers.UUIDField()
    registration_number = serializers.CharField()
    availability_status = serializers.CharField()
    vehicle_type = serializers.CharField(allow_blank=True)
    client = serializers.CharField(allow_blank=True)
    driver_name = serializers.CharField(allow_null=True)
    latitude = serializers.DecimalField(max_digits=9, decimal_places=6)
    longitude = serializers.DecimalField(max_digits=9, decimal_places=6)
    speed = serializers.DecimalField(max_digits=6, decimal_places=2, allow_null=True)
    heading = serializers.IntegerField(allow_null=True)
    ignition = serializers.BooleanField(allow_null=True)
    odometer = serializers.DecimalField(max_digits=10, decimal_places=1, allow_null=True)
    location = serializers.CharField(allow_blank=True)
    gps_odometer = serializers.DecimalField(max_digits=12, decimal_places=3, allow_null=True)
    timestamp = serializers.DateTimeField()
    connection_status = serializers.CharField()
    movement_state = serializers.CharField(allow_null=True)
    active_trip = ActiveTripSerializer(allow_null=True)


class TripReportSerializer(serializers.Serializer):
    """Feeds the Trip Report page — each item is a plain dict from
    apps.tracking.trip_report.serialize_trips(), one ignition-derived trip
    computed from TelemetryEvent history (never a stored row)."""

    trip_id = serializers.CharField()
    vehicle_uuid = serializers.UUIDField()
    registration_number = serializers.CharField()
    vehicle_type = serializers.CharField(allow_blank=True)
    driver_name = serializers.CharField(allow_null=True)
    status = serializers.CharField()
    ignition = serializers.BooleanField(allow_null=True)
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField(allow_null=True)
    duration_seconds = serializers.IntegerField()
    start_location = serializers.CharField(allow_blank=True)
    end_location = serializers.CharField(allow_blank=True)
    start_latitude = serializers.DecimalField(max_digits=9, decimal_places=6, allow_null=True)
    start_longitude = serializers.DecimalField(max_digits=9, decimal_places=6, allow_null=True)
    end_latitude = serializers.DecimalField(max_digits=9, decimal_places=6, allow_null=True)
    end_longitude = serializers.DecimalField(max_digits=9, decimal_places=6, allow_null=True)
    distance_km = serializers.DecimalField(max_digits=10, decimal_places=1, allow_null=True)


class TripRoutePointSerializer(serializers.Serializer):
    t = serializers.DateTimeField()
    lat = serializers.DecimalField(max_digits=9, decimal_places=6)
    lon = serializers.DecimalField(max_digits=9, decimal_places=6)


class TripRouteSerializer(serializers.Serializer):
    """Feeds the Map column's expanded view — the real GPS points for one
    specific trip (apps.tracking.trip_report.trip_route()), fetched only
    when that trip is drawn/opened, never for the whole day-wise list."""

    vehicle_uuid = serializers.UUIDField()
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField()
    truncated = serializers.BooleanField()
    points = TripRoutePointSerializer(many=True)
