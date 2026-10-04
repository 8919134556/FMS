from django.contrib import admin

from apps.maintenance.models import Maintenance, MaintenanceNumberSequence, MaintenancePart


class MaintenancePartInline(admin.TabularInline):
    model = MaintenancePart
    extra = 0


@admin.register(Maintenance)
class MaintenanceAdmin(admin.ModelAdmin):
    list_display = ("maintenance_number", "vehicle", "maintenance_type", "status", "priority", "scheduled_date")
    list_filter = ("status", "maintenance_type", "priority")
    search_fields = ("maintenance_number", "service_center")
    inlines = [MaintenancePartInline]

    def get_queryset(self, request):
        return Maintenance.all_objects.all()


@admin.register(MaintenanceNumberSequence)
class MaintenanceNumberSequenceAdmin(admin.ModelAdmin):
    list_display = ("id", "last_value")
