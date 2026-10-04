import pytest
from django.urls import reverse

from apps.accounts.models import Permission
from apps.accounts.tests.factories import UserFactory
from apps.core.models import SystemSettings
from apps.core.tests.factories import role_with

pytestmark = pytest.mark.django_db


class TestSettingsAccess:
    def test_anonymous_redirected_to_login(self, client):
        response = client.get(reverse("core:settings"))
        assert response.status_code == 302

    def test_user_without_permission_gets_403(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("core:settings"))
        assert response.status_code == 403

    def test_view_only_user_sees_form_but_not_save_button(self, client):
        role = role_with((Permission.Module.SETTINGS, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        client.force_login(viewer)
        response = client.get(reverse("core:settings"))
        assert response.status_code == 200
        assert b"Save Settings" not in response.content
        assert b"view-only access" in response.content

    def test_view_only_user_cannot_post(self, client):
        role = role_with((Permission.Module.SETTINGS, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        client.force_login(viewer)
        response = client.post(reverse("core:settings"), {"company_name": "Hacked Co"})
        assert response.status_code == 403
        assert SystemSettings.load().company_name != "Hacked Co"


class TestSettingsUpdate:
    def test_update_persists_and_logs_audit(self, client):
        role = role_with(
            (Permission.Module.SETTINGS, Permission.Action.VIEW),
            (Permission.Module.SETTINGS, Permission.Action.UPDATE),
        )
        actor = UserFactory(role=role)
        client.force_login(actor)
        response = client.post(
            reverse("core:settings"),
            {
                "company_name": "Acme Logistics",
                "support_email": "support@acme.test",
                "support_phone": "+91 90000 00000",
                "default_timezone": "Asia/Kolkata",
                "default_currency": SystemSettings.Currency.INR,
                "date_format": SystemSettings.DateFormat.DMY,
                "document_expiry_warning_days": 45,
                "maintenance_due_soon_days": 10,
            },
        )
        assert response.status_code == 302
        settings_obj = SystemSettings.load()
        assert settings_obj.company_name == "Acme Logistics"
        assert settings_obj.document_expiry_warning_days == 45
        assert settings_obj.maintenance_due_soon_days == 10
        assert settings_obj.updated_by == actor

        from apps.audit.models import AuditLog

        assert AuditLog.objects.filter(module="settings", action=AuditLog.Action.UPDATE).exists()

    def test_company_name_overrides_login_page_branding(self, client):
        settings_obj = SystemSettings.load()
        settings_obj.company_name = "Custom Branded Fleet"
        settings_obj.save()
        response = client.get(reverse("accounts:login"))
        assert b"Custom Branded Fleet" in response.content

    def test_invalid_form_does_not_save(self, client):
        role = role_with(
            (Permission.Module.SETTINGS, Permission.Action.VIEW),
            (Permission.Module.SETTINGS, Permission.Action.UPDATE),
        )
        actor = UserFactory(role=role)
        client.force_login(actor)
        response = client.post(
            reverse("core:settings"),
            {
                "support_email": "not-an-email",
                "default_timezone": "Asia/Kolkata",
                "default_currency": SystemSettings.Currency.INR,
                "date_format": SystemSettings.DateFormat.DMY,
                "document_expiry_warning_days": 30,
                "maintenance_due_soon_days": 7,
            },
        )
        assert response.status_code == 200
        assert b"Enter a valid email address" in response.content or response.context["form"].errors


class TestSettingsSingleton:
    def test_load_returns_the_pre_seeded_row(self):
        # A data migration seeds pk=1 so every request-time site_meta lookup
        # (see apps.core.context_processors.site_meta) is a plain SELECT,
        # never a lazy get_or_create INSERT on whichever request runs first.
        assert SystemSettings.objects.count() == 1
        settings_obj = SystemSettings.load()
        assert settings_obj.pk == 1

    def test_load_returns_same_row_on_repeat_calls(self):
        first = SystemSettings.load()
        first.company_name = "Persisted Co"
        first.save()
        second = SystemSettings.load()
        assert second.pk == first.pk
        assert second.company_name == "Persisted Co"
