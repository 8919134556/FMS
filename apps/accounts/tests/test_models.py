import pytest
from django.db import IntegrityError

from apps.accounts.models import Permission, RolePermission, User
from apps.accounts.tests.factories import PermissionFactory, RoleFactory, UserFactory

pytestmark = pytest.mark.django_db


class TestUserModel:
    def test_str_and_full_name(self):
        user = UserFactory(first_name="Ada", middle_name="", last_name="Lovelace", username="ada")
        assert user.get_full_name() == "Ada Lovelace"
        assert "ada" in str(user)

    def test_status_inactive_syncs_is_active_false(self):
        user = UserFactory(status=User.Status.ACTIVE)
        assert user.is_active is True
        user.status = User.Status.SUSPENDED
        user.save()
        user.refresh_from_db()
        assert user.is_active is False

    def test_status_invited_is_still_active(self):
        user = UserFactory(status=User.Status.INVITED)
        assert user.is_active is True

    def test_soft_delete_excludes_from_default_manager(self):
        user = UserFactory()
        user.delete()
        assert not User.objects.filter(pk=user.pk).exists()
        assert User.all_objects.filter(pk=user.pk).exists()
        user.refresh_from_db()
        assert user.is_deleted is True
        assert user.status == User.Status.INACTIVE

    def test_restore(self):
        user = UserFactory()
        user.delete()
        user.restore()
        assert User.objects.filter(pk=user.pk).exists()
        assert user.status == User.Status.ACTIVE

    def test_employee_id_unique(self):
        UserFactory(employee_id="EMP00001")
        with pytest.raises(IntegrityError):
            UserFactory(employee_id="EMP00001")

    def test_password_is_hashed(self):
        user = UserFactory()
        assert user.password != "TestPass!1234"
        assert user.check_password("TestPass!1234")


class TestRoleAndPermission:
    def test_permission_code_is_derived(self):
        permission = PermissionFactory(module=Permission.Module.DRIVER, action=Permission.Action.CREATE)
        assert permission.code == "driver.create"

    def test_permission_module_action_unique(self):
        PermissionFactory(module=Permission.Module.CLIENT, action=Permission.Action.VIEW)
        with pytest.raises(IntegrityError):
            PermissionFactory.build(module=Permission.Module.CLIENT, action=Permission.Action.VIEW).save()

    def test_role_permission_grants_access(self):
        role = RoleFactory()
        permission = PermissionFactory(module=Permission.Module.VEHICLE, action=Permission.Action.CREATE)
        RolePermission.objects.create(role=role, permission=permission)
        assert role.permissions.filter(code="vehicle.create").exists()

    def test_role_permission_unique_together(self):
        role = RoleFactory()
        permission = PermissionFactory(module=Permission.Module.TRIP, action=Permission.Action.VIEW)
        RolePermission.objects.create(role=role, permission=permission)
        with pytest.raises(IntegrityError):
            RolePermission.objects.create(role=role, permission=permission)
