from django.contrib import admin

from apps.drivers.models import Driver


@admin.register(Driver)
class DriverAdmin(admin.ModelAdmin):
    list_display = ("employee_id", "first_name", "last_name", "license_number", "employment_status")
    list_filter = ("employment_status", "driver_type", "employment_type")
    search_fields = ("employee_id", "first_name", "last_name", "license_number", "mobile_number")

    def get_queryset(self, request):
        return Driver.all_objects.all()
