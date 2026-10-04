import pytest
from django.urls import reverse

from apps.accounts.models import Permission, RolePermission, User
from apps.accounts.tests.factories import RoleFactory, UserFactory
from apps.audit.models import AuditLog

pytestmark = pytest.mark.django_db


def _role_with(*module_actions):
    role = RoleFactory()
    for module, action in module_actions:
        permission, _ = Permission.objects.get_or_create(module=module, action=action)
        RolePermission.objects.create(role=role, permission=permission)
    return role


class TestLoginFlow:
    def test_login_page_renders(self, client):
        response = client.get(reverse("accounts:login"))
        assert response.status_code == 200

    def test_successful_login_creates_audit_log(self, client):
        user = UserFactory(username="alice")
        response = client.post(reverse("accounts:login"), {"username": "alice", "password": "TestPass!1234"})
        assert response.status_code == 302
        assert AuditLog.objects.filter(action=AuditLog.Action.LOGIN, entity_id=str(user.pk)).exists()

    def test_locked_user_cannot_reach_dashboard(self, client):
        UserFactory(username="bob", status=User.Status.LOCKED)
        client.post(reverse("accounts:login"), {"username": "bob", "password": "TestPass!1234"})
        response = client.get(reverse("core:dashboard"))
        assert response.status_code in (302, 403)


class TestUserListView:
    def test_anonymous_redirected_to_login(self, client):
        response = client.get(reverse("accounts_portal:user_list"))
        assert response.status_code == 302
        assert reverse("accounts:login") in response.url

    def test_user_without_permission_gets_403(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("accounts_portal:user_list"))
        assert response.status_code == 403

    def test_user_with_view_permission_sees_list(self, client):
        role = _role_with((Permission.Module.USER, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        UserFactory.create_batch(3)
        client.force_login(viewer)
        response = client.get(reverse("accounts_portal:user_list"))
        assert response.status_code == 200
        assert b"Users" in response.content

    def test_search_filters_results(self, client):
        role = _role_with((Permission.Module.USER, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        UserFactory(first_name="Zebediah", username="zeb")
        client.force_login(viewer)
        response = client.get(reverse("accounts_portal:user_list"), {"q": "Zebediah"})
        assert response.status_code == 200
        assert b"Zebediah" in response.content


class TestUserCreateView:
    def test_create_user_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("accounts_portal:user_create"))
        assert response.status_code == 403

    def test_create_user_success(self, client):
        role = _role_with(
            (Permission.Module.USER, Permission.Action.VIEW),
            (Permission.Module.USER, Permission.Action.CREATE),
        )
        actor = UserFactory(role=role)
        client.force_login(actor)
        response = client.post(
            reverse("accounts_portal:user_create"),
            {
                "employee_id": "EMP99999",
                "username": "newhire",
                "first_name": "New",
                "last_name": "Hire",
                "email": "newhire@example.com",
                "mobile_number": "+15551234567",
                "status": User.Status.ACTIVE,
                "password1": "SuperSecret!123",
                "password2": "SuperSecret!123",
            },
        )
        assert response.status_code == 302
        created = User.objects.get(username="newhire")
        assert created.check_password("SuperSecret!123")
        assert AuditLog.objects.filter(action=AuditLog.Action.CREATE, entity_id=str(created.pk)).exists()


class TestUserLifecycleActions:
    def test_deactivate_user(self, client):
        role = _role_with(
            (Permission.Module.USER, Permission.Action.VIEW),
            (Permission.Module.USER, Permission.Action.ARCHIVE),
        )
        actor = UserFactory(role=role)
        target = UserFactory(status=User.Status.ACTIVE)
        client.force_login(actor)
        response = client.post(reverse("accounts_portal:user_deactivate", kwargs={"uuid": target.uuid}))
        assert response.status_code == 302
        target.refresh_from_db()
        assert target.status == User.Status.INACTIVE
        assert target.is_active is False

    def test_deactivate_requires_get_not_allowed(self, client):
        role = _role_with((Permission.Module.USER, Permission.Action.ARCHIVE))
        actor = UserFactory(role=role)
        target = UserFactory()
        client.force_login(actor)
        response = client.get(reverse("accounts_portal:user_deactivate", kwargs={"uuid": target.uuid}))
        assert response.status_code == 405


class TestRoleViews:
    def test_role_list_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("accounts_portal:role_list"))
        assert response.status_code == 403

    def test_update_role_permissions(self, client):
        role = _role_with(
            (Permission.Module.ROLE, Permission.Action.VIEW),
            (Permission.Module.ROLE, Permission.Action.UPDATE),
        )
        actor = UserFactory(role=role)
        target_role = RoleFactory()
        permission = Permission.objects.create(module=Permission.Module.VEHICLE, action=Permission.Action.VIEW)
        client.force_login(actor)
        response = client.post(
            reverse("accounts_portal:role_update_permissions", kwargs={"uuid": target_role.uuid}),
            {"permissions": [permission.id]},
        )
        assert response.status_code == 302
        assert target_role.permissions.filter(pk=permission.pk).exists()
