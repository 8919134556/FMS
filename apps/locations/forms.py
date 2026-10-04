from django import forms

from apps.core.forms import BootstrapFormMixin
from apps.locations.models import Branch, Site

SITE_FIELDS = [
    "site_code", "site_name", "site_type", "client", "branch",
    "contact_name", "contact_phone", "contact_email",
    "address_line_1", "city", "state", "country", "pincode",
    "latitude", "longitude", "operating_hours", "status", "notes",
]

OPTIONAL_SITE_FIELDS = [
    "branch", "contact_name", "contact_phone", "contact_email",
    "address_line_1", "city", "state", "country", "pincode",
    "latitude", "longitude", "operating_hours", "notes",
]


class SiteForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Site
        fields = SITE_FIELDS
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in OPTIONAL_SITE_FIELDS:
            self.fields[name].required = False

    def save(self, commit=True, actor=None):
        site = super().save(commit=False)
        if not site.pk:
            site.created_by = actor
        site.updated_by = actor
        if commit:
            site.save()
        return site


BRANCH_FIELDS = [
    "code", "name", "branch_type", "client", "manager",
    "address", "city", "state", "country", "pincode", "phone", "email", "status",
]

OPTIONAL_BRANCH_FIELDS = ["client", "manager", "address", "city", "state", "country", "pincode", "phone", "email"]


class BranchForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Branch
        fields = BRANCH_FIELDS
        widgets = {
            "address": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in OPTIONAL_BRANCH_FIELDS:
            self.fields[name].required = False

    def save(self, commit=True, actor=None):
        branch = super().save(commit=False)
        if not branch.pk:
            branch.created_by = actor
        branch.updated_by = actor
        if commit:
            branch.save()
        return branch
