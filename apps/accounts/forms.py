from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.password_validation import validate_password

from apps.clients.models import Client
from apps.accounts.models import Role, User
from apps.core.forms import BootstrapFormMixin


class LoginForm(BootstrapFormMixin, AuthenticationForm):
    remember_me = forms.BooleanField(label="Remember me", required=False, initial=True)

    error_messages = {
        "invalid_login": "Enter a correct username and password. Note that the password is case-sensitive.",
        "inactive": "This account is inactive. Contact your administrator.",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update(
            {"placeholder": "Email or Username", "autocomplete": "username", "autofocus": True}
        )
        self.fields["password"].widget.attrs.update({"placeholder": "Password", "autocomplete": "current-password"})


class UserCreateForm(BootstrapFormMixin, forms.ModelForm):
    password1 = forms.CharField(label="Password", widget=forms.PasswordInput, help_text="Minimum 10 characters.")
    password2 = forms.CharField(label="Confirm password", widget=forms.PasswordInput)

    class Meta:
        model = User
        fields = [
            "employee_id",
            "username",
            "first_name",
            "middle_name",
            "last_name",
            "email",
            "mobile_number",
            "alternate_mobile_number",
            "profile_photo",
            "date_of_birth",
            "gender",
            "designation",
            "department",
            "role",
            "client",
            "reporting_manager",
            "address",
            "city",
            "state",
            "country",
            "pincode",
            "date_of_joining",
            "status",
        ]
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}),
            "date_of_joining": forms.DateInput(attrs={"type": "date"}),
            "address": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].queryset = Role.objects.filter(is_active=True)
        self.fields["client"].queryset = Client.objects.filter(status=Client.Status.ACTIVE)
        self.fields["client"].label = "Client (client user only)"
        self.fields["client"].help_text = (
            "Link this account to a client to make it a client user: it will see only that client's "
            "vehicles, trips and data, read-only. Leave empty for internal staff."
        )
        self.fields["reporting_manager"].queryset = User.objects.all()
        for name in ("middle_name", "alternate_mobile_number", "date_of_birth", "gender", "designation",
                     "department", "role", "client", "reporting_manager", "address", "city", "state", "country",
                     "pincode", "date_of_joining", "profile_photo"):
            self.fields[name].required = False

    def clean(self):
        cleaned = super().clean()
        password1 = cleaned.get("password1")
        password2 = cleaned.get("password2")
        if password1 and password2 and password1 != password2:
            self.add_error("password2", "The two password fields didn't match.")
        elif password1:
            validate_password(password1)
        return cleaned

    def save(self, commit=True, created_by=None):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password1"])
        user.created_by = created_by
        user.updated_by = created_by
        if commit:
            user.save()
        return user


class UserUpdateForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = User
        fields = [
            "employee_id",
            "username",
            "first_name",
            "middle_name",
            "last_name",
            "email",
            "mobile_number",
            "alternate_mobile_number",
            "profile_photo",
            "date_of_birth",
            "gender",
            "designation",
            "department",
            "role",
            "client",
            "reporting_manager",
            "address",
            "city",
            "state",
            "country",
            "pincode",
            "date_of_joining",
            "status",
        ]
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}),
            "date_of_joining": forms.DateInput(attrs={"type": "date"}),
            "address": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].queryset = Role.objects.filter(is_active=True)
        self.fields["client"].queryset = Client.objects.filter(status=Client.Status.ACTIVE)
        self.fields["client"].label = "Client (client user only)"
        self.fields["client"].help_text = (
            "Link this account to a client to make it a client user: it will see only that client's "
            "vehicles, trips and data, read-only. Leave empty for internal staff."
        )
        self.fields["reporting_manager"].queryset = User.objects.exclude(pk=self.instance.pk)
        for name in ("middle_name", "alternate_mobile_number", "date_of_birth", "gender", "designation",
                     "department", "role", "client", "reporting_manager", "address", "city", "state", "country",
                     "pincode", "date_of_joining", "profile_photo"):
            self.fields[name].required = False

    def clean_reporting_manager(self):
        manager = self.cleaned_data.get("reporting_manager")
        if manager and self.instance.pk and manager.pk == self.instance.pk:
            raise forms.ValidationError("A user cannot be their own reporting manager.")
        return manager

    def save(self, commit=True, updated_by=None):
        user = super().save(commit=False)
        user.updated_by = updated_by
        if commit:
            user.save()
        return user


class RoleForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Role
        fields = ["name", "code", "description", "is_active"]

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk and self.instance.is_system_role:
            if cleaned.get("code") != self.instance.code:
                self.add_error("code", "The code of a system role cannot be changed.")
        return cleaned
