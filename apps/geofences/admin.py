from django.contrib import admin

from apps.geofences.models import Geofence, GeofenceEvent


@admin.register(Geofence)
class GeofenceAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "radius_meters", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("code", "name")

    def get_queryset(self, request):
        return Geofence.all_objects.all()


@admin.register(GeofenceEvent)
class GeofenceEventAdmin(admin.ModelAdmin):
    list_display = ("geofence", "vehicle", "event_type", "occurred_at")
    list_filter = ("event_type",)
    search_fields = ("geofence__name", "vehicle__registration_number")
