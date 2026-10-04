from django.contrib import admin

from apps.vendors.models import Vendor


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ("vendor_code", "vendor_name", "vendor_type", "status", "created_at")
    list_filter = ("status", "vendor_type")
    search_fields = ("vendor_code", "vendor_name", "email")

    def get_queryset(self, request):
        return Vendor.all_objects.all()
