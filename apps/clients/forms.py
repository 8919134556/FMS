from django import forms

from apps.clients.models import Client
from apps.core.forms import BootstrapFormMixin

CLIENT_FIELDS = [
    "client_code", "client_name", "legal_name", "client_type", "industry",
    "email", "phone",
    "address_line_1", "city", "state", "country", "pincode",
    "account_manager", "status", "notes",
]

OPTIONAL_CLIENT_FIELDS = [
    "legal_name", "industry", "email", "phone",
    "address_line_1", "city", "state", "country", "pincode",
    "account_manager", "notes",
]


class ClientForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Client
        fields = CLIENT_FIELDS
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in OPTIONAL_CLIENT_FIELDS:
            self.fields[name].required = False

    def save(self, commit=True, actor=None):
        client = super().save(commit=False)
        if not client.pk:
            client.created_by = actor
        client.updated_by = actor
        if commit:
            client.save()
        return client
