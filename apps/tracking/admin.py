from django.contrib import admin

from apps.tracking.models import DeviceCommand, RawTelemetryEvent, TrackingDevice

# TelemetryEvent/VehicleCurrentTelemetry are deliberately NOT registered here
# — high-volume/single-row-per-vehicle tables respectively, better inspected
# via the Device/Vehicle detail pages than a slow admin changelist.


@admin.register(TrackingDevice)
class TrackingDeviceAdmin(admin.ModelAdmin):
    list_display = ("imei", "name", "provider", "device_model", "vehicle", "status", "last_communication")
    list_filter = ("status", "provider")
    search_fields = ("imei", "serial_number", "name")

    def get_queryset(self, request):
        return TrackingDevice.all_objects.all()


@admin.register(RawTelemetryEvent)
class RawTelemetryEventAdmin(admin.ModelAdmin):
    list_display = ("device", "provider", "received_at", "processing_status")
    list_filter = ("processing_status", "provider")
    search_fields = ("device__imei",)
    readonly_fields = ("device", "provider", "received_at", "payload", "processing_status", "error_message")

    def has_add_permission(self, request):
        return False


@admin.register(DeviceCommand)
class DeviceCommandAdmin(admin.ModelAdmin):
    """Read-only in admin — all real mutation goes through
    apps.tracking.command_services (state machine + audit logging), never
    a bare admin save."""

    list_display = ("device", "command_type", "status", "created_at", "sent_at", "retry_count")
    list_filter = ("status", "command_type")
    search_fields = ("device__imei",)
    readonly_fields = [f.name for f in DeviceCommand._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
