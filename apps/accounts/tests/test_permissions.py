import pytest

from apps.accounts.models import Permission, RolePermission
from apps.accounts.tests.factories import PermissionFactory, RoleFactory, UserFactory
from apps.core.permissions import user_has_permission

pytestmark = pytest.mark.django_db


def test_superuser_always_has_permission():
    user = UserFactory(is_superuser=True)
    assert user_has_permission(user, "vehicle", "delete") is True


def test_user_without_role_has_no_permission():
    user = UserFactory(role=None)
    assert user_has_permission(user, "vehicle", "view") is False


def test_user_with_role_and_matching_permission():
    role = RoleFactory()
    permission = PermissionFactory(module=Permission.Module.VEHICLE, action=Permission.Action.VIEW)
    RolePermission.objects.create(role=role, permission=permission)
    user = UserFactory(role=role)
    assert user_has_permission(user, "vehicle", "view") is True


def test_user_with_role_but_missing_permission():
    role = RoleFactory()
    user = UserFactory(role=role)
    assert user_has_permission(user, "vehicle", "delete") is False


def test_inactive_role_denies_permission():
    role = RoleFactory(is_active=False)
    permission = PermissionFactory(module=Permission.Module.VEHICLE, action=Permission.Action.VIEW)
    RolePermission.objects.create(role=role, permission=permission)
    user = UserFactory(role=role)
    assert user_has_permission(user, "vehicle", "view") is False


def test_anonymous_user_has_no_permission():
    from django.contrib.auth.models import AnonymousUser

    assert user_has_permission(AnonymousUser(), "vehicle", "view") is False
