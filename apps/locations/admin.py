from django.contrib import admin

from apps.locations.models import Branch, Site


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "branch_type", "client", "status", "created_at")
    list_filter = ("status", "branch_type")
    search_fields = ("code", "name")

    def get_queryset(self, request):
        return Branch.all_objects.all()


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = ("site_code", "site_name", "site_type", "client", "branch", "status")
    list_filter = ("status", "site_type")
    search_fields = ("site_code", "site_name")

    def get_queryset(self, request):
        return Site.all_objects.all()
