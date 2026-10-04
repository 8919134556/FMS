from django import forms

from apps.core.forms import BootstrapFormMixin
from apps.vendors.models import Vendor

VENDOR_FIELDS = [
    "vendor_code", "vendor_name", "vendor_type", "contact_person",
    "email", "phone", "address", "city", "state", "country", "pincode", "status",
]

OPTIONAL_VENDOR_FIELDS = ["contact_person", "email", "phone", "address", "city", "state", "country", "pincode"]


class VendorForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Vendor
        fields = VENDOR_FIELDS
        widgets = {
            "address": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in OPTIONAL_VENDOR_FIELDS:
            self.fields[name].required = False

    def save(self, commit=True, actor=None):
        vendor = super().save(commit=False)
        if not vendor.pk:
            vendor.created_by = actor
        vendor.updated_by = actor
        if commit:
            vendor.save()
        return vendor
