from django import forms
from django.db import models

from apps.accounts.models import Role
from apps.core.forms import BootstrapFormMixin
from apps.notifications.models import Notification


class BroadcastNotificationForm(BootstrapFormMixin, forms.Form):
    class Audience(models.TextChoices):
        ALL_USERS = "ALL_USERS", "All Users"
        SPECIFIC_ROLE = "SPECIFIC_ROLE", "Users with a specific role"

    audience = forms.ChoiceField(choices=Audience.choices, initial=Audience.ALL_USERS)
    role = forms.ModelChoiceField(queryset=Role.objects.filter(is_active=True), required=False)
    title = forms.CharField(max_length=200)
    body = forms.CharField(max_length=500, required=False, widget=forms.Textarea(attrs={"rows": 3}))
    level = forms.ChoiceField(choices=Notification.Level.choices, initial=Notification.Level.INFO)
    link_url = forms.CharField(max_length=300, required=False)

    def clean_link_url(self):
        # Rendered as an <a href> in every recipient's bell/inbox, so a
        # "javascript:" (or other odd-scheme) URL would be a stored-XSS vector.
        value = self.cleaned_data["link_url"].strip()
        if value and not (value.startswith("/") and not value.startswith("//")) and not value.lower().startswith(("http://", "https://")):
            raise forms.ValidationError("Use a site-relative path (e.g. /trips/) or an http(s) URL.")
        return value

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("audience") == self.Audience.SPECIFIC_ROLE and not cleaned.get("role"):
            self.add_error("role", "Select a role, or choose 'All Users' instead.")
        return cleaned
