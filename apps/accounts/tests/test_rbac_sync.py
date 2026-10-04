import pytest
from django.apps import apps as django_apps
from django.db.models.signals import post_migrate

from apps.accounts.models import Permission, Role, RolePermission
from apps.accounts.tests.factories import RoleFactory

pytestmark = pytest.mark.django_db


def _fire_post_migrate():
    config = django_apps.get_app_config("accounts")
    post_migrate.send(sender=config, app_config=config, verbosity=0, interactive=False, using="default")


def test_admin_roles_get_every_missing_permission_but_admin_never_gets_delete():
    super_admin = RoleFactory(code="super-admin", name="SA")
    admin = RoleFactory(code="admin", name="AD")
    _fire_post_migrate()
    assert super_admin.permissions.filter(action="delete").exists()
    assert not admin.permissions.filter(action="delete").exists()
    assert admin.permissions.filter(module="settings", action="update").exists()
    assert super_admin.permissions.count() == Permission.objects.count()


def test_other_roles_are_never_modified():
    RoleFactory(code="admin", name="AD")
    custom = RoleFactory(code="custom-role", name="Custom")
    perm, _ = Permission.objects.get_or_create(module="vehicle", action="view")
    RolePermission.objects.create(role=custom, permission=perm)
    _fire_post_migrate()
    assert list(custom.permissions.values_list("code", flat=True)) == ["vehicle.view"]


def test_no_admin_roles_means_nothing_is_created():
    before = Permission.objects.count()
    _fire_post_migrate()
    assert Permission.objects.count() == before
    assert not Role.objects.filter(code__in=["admin", "super-admin"]).exists()
