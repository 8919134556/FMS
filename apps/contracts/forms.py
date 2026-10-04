from django import forms

from apps.clients.models import Client
from apps.contracts.models import Contract
from apps.core.forms import BootstrapFormMixin
from apps.vendors.models import Vendor

CONTRACT_FIELDS = [
    "contract_number", "client", "vendor", "contract_type",
    "start_date", "end_date", "contract_value", "status",
]

OPTIONAL_CONTRACT_FIELDS = ["client", "vendor", "start_date", "end_date", "contract_value"]


class ContractForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Contract
        fields = CONTRACT_FIELDS
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in OPTIONAL_CONTRACT_FIELDS:
            self.fields[name].required = False
        self.fields["client"].queryset = Client.objects.filter(status=Client.Status.ACTIVE)
        self.fields["vendor"].queryset = Vendor.objects.filter(status=Vendor.Status.ACTIVE)

    def clean(self):
        cleaned_data = super().clean()
        start_date = cleaned_data.get("start_date")
        end_date = cleaned_data.get("end_date")
        if start_date and end_date and end_date <= start_date:
            self.add_error("end_date", "End date must be after start date.")
        return cleaned_data

    def save(self, commit=True, actor=None):
        contract = super().save(commit=False)
        if not contract.pk:
            contract.created_by = actor
        contract.updated_by = actor
        if commit:
            contract.save()
        return contract
