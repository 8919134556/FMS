from django.contrib import admin

from apps.clients.models import Client


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("client_code", "client_name", "client_type", "status", "created_at")
    list_filter = ("status", "client_type")
    search_fields = ("client_code", "client_name", "legal_name", "email")

    def get_queryset(self, request):
        return Client.all_objects.all()
