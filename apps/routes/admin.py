from django.contrib import admin

from apps.routes.models import Route, RouteStop


class RouteStopInline(admin.TabularInline):
    model = RouteStop
    extra = 1


@admin.register(Route)
class RouteAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "client", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("code", "name")
    inlines = [RouteStopInline]

    def get_queryset(self, request):
        return Route.all_objects.all()
