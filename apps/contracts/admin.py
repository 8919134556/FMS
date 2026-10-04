from django.contrib import admin

from apps.contracts.models import Contract


@admin.register(Contract)
class ContractAdmin(admin.ModelAdmin):
    list_display = ("contract_number", "client", "vendor", "contract_type", "status", "end_date")
    list_filter = ("status", "contract_type")
    search_fields = ("contract_number",)

    def get_queryset(self, request):
        return Contract.all_objects.all()
