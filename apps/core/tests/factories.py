"""Shared helper for granting a role a set of module permissions in tests —
mirrors the ``_role_with`` helper duplicated across every other app's test
suite, centralized here since apps.core.tests exercises every module at once."""

from apps.accounts.models import Permission, RolePermission
from apps.accounts.tests.factories import RoleFactory


def role_with(*module_actions):
    role = RoleFactory()
    for module, action in module_actions:
        permission, _ = Permission.objects.get_or_create(module=module, action=action)
        RolePermission.objects.create(role=role, permission=permission)
    return role
