from django import forms

from apps.core.models import SystemSettings


class BootstrapFormMixin:
    """Applies Bootstrap 5 form-control/form-select/form-check classes to every field.

    Mix this in ahead of ``forms.Form``/``forms.ModelForm`` so templates can render
    fields directly (``{{ form.username }}``) without needing django-crispy-forms
    or repeating widget attrs on every field declaration.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                css_class = "form-check-input"
            elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
                css_class = "form-select"
            elif isinstance(widget, forms.ClearableFileInput):
                css_class = "form-control"
            else:
                css_class = "form-control"
            existing = widget.attrs.get("class", "")
            widget.attrs["class"] = f"{existing} {css_class}".strip()


class SystemSettingsForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = SystemSettings
        fields = [
            "company_name", "support_email", "support_phone",
            "default_timezone", "default_currency", "date_format",
            "document_expiry_warning_days", "maintenance_due_soon_days",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("company_name", "support_email", "support_phone"):
            self.fields[name].required = False
