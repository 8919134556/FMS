from django.contrib import admin

from apps.trips.models import Trip, TripNumberSequence


@admin.register(Trip)
class TripAdmin(admin.ModelAdmin):
    list_display = ("trip_number", "client", "origin_site", "destination_site", "vehicle", "driver", "status", "scheduled_start")
    list_filter = ("status", "trip_type", "priority")
    search_fields = ("trip_number",)

    def get_queryset(self, request):
        return Trip.all_objects.all()


@admin.register(TripNumberSequence)
class TripNumberSequenceAdmin(admin.ModelAdmin):
    list_display = ("id", "last_value")
