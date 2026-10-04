from django.contrib import admin

from apps.vehicles.models import FuelType, Vehicle, VehicleCategory, VehicleDriverAssignment, VehicleType


@admin.register(VehicleCategory)
class VehicleCategoryAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_active")
    search_fields = ("code", "name")


@admin.register(VehicleType)
class VehicleTypeAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "category", "is_active")
    list_filter = ("category", "is_active")
    search_fields = ("code", "name")


@admin.register(FuelType)
class FuelTypeAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_active")
    search_fields = ("code", "name")


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ("registration_number", "vehicle_code", "make", "model", "status", "availability_status")
    list_filter = ("status", "availability_status", "vehicle_type", "fuel_type")
    search_fields = ("registration_number", "vehicle_code", "vin", "chassis_number", "engine_number")

    def get_queryset(self, request):
        return Vehicle.all_objects.all()


@admin.register(VehicleDriverAssignment)
class VehicleDriverAssignmentAdmin(admin.ModelAdmin):
    list_display = ("vehicle", "driver", "assignment_type", "primary_driver", "status", "start_date", "end_date")
    list_filter = ("status", "assignment_type", "primary_driver")

    def get_queryset(self, request):
        return VehicleDriverAssignment.all_objects.all()
