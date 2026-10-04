"""Authentication views: login, logout, and password management.

Mounted at ``/accounts/`` (see ``apps/accounts/urls.py``). CRUD for the User
and Role *entities* lives in ``portal_views.py``, mounted at ``/admin/``.
"""

from django.contrib.auth import views as auth_views
from django.urls import reverse_lazy

from apps.accounts.forms import LoginForm


class FMSLoginView(auth_views.LoginView):
    template_name = "registration/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True

    def form_valid(self, form):
        response = super().form_valid(form)
        if not form.cleaned_data.get("remember_me"):
            # Session cookie expires when the browser closes instead of
            # riding out the full SESSION_COOKIE_AGE.
            self.request.session.set_expiry(0)
        return response


class FMSLogoutView(auth_views.LogoutView):
    next_page = reverse_lazy("accounts:login")


class FMSPasswordChangeView(auth_views.PasswordChangeView):
    template_name = "registration/password_change_form.html"
    success_url = reverse_lazy("accounts:password_change_done")


class FMSPasswordChangeDoneView(auth_views.PasswordChangeDoneView):
    template_name = "registration/password_change_done.html"


class FMSPasswordResetView(auth_views.PasswordResetView):
    template_name = "registration/password_reset_form.html"
    email_template_name = "registration/password_reset_email.txt"
    subject_template_name = "registration/password_reset_subject.txt"
    success_url = reverse_lazy("accounts:password_reset_done")


class FMSPasswordResetDoneView(auth_views.PasswordResetDoneView):
    template_name = "registration/password_reset_done.html"


class FMSPasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = "registration/password_reset_confirm.html"
    success_url = reverse_lazy("accounts:password_reset_complete")


class FMSPasswordResetCompleteView(auth_views.PasswordResetCompleteView):
    template_name = "registration/password_reset_complete.html"
