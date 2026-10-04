from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.db.models.signals import post_migrate
from django.dispatch import receiver

from apps.audit.models import AuditLog
from apps.audit.services import log_action


@receiver(user_logged_in)
def on_user_logged_in(sender, request, user, **kwargs):
    log_action(
        action=AuditLog.Action.LOGIN,
        module="auth",
        entity="User",
        entity_id=str(user.pk),
        user=user,
        request=request,
    )


@receiver(user_logged_out)
def on_user_logged_out(sender, request, user, **kwargs):
    if user is None:
        return
    log_action(
        action=AuditLog.Action.LOGOUT,
        module="auth",
        entity="User",
        entity_id=str(user.pk),
        user=user,
        request=request,
    )


@receiver(post_migrate)
def top_up_admin_role_permissions(sender, **kwargs):
    """Admin and Super Admin must always reach every module. When a release
    adds a module (new Permission rows), those two roles would otherwise
    lack it until someone remembered to re-run ``seed_roles`` — so after
    every migrate, grant them anything missing.

    Deliberately narrow: it only ever *adds* permissions, only to these two
    system roles, and does nothing if they don't exist yet (a fresh DB with
    no roles seeded is left alone). Every other role is untouched — their
    permissions are administrator-managed and must not be overwritten.
    """
    if getattr(sender, "label", None) != "accounts":
        return

    from apps.accounts.management.commands.seed_roles import MODULE_ACTIONS
    from apps.accounts.models import Permission, Role, RolePermission

    roles = list(Role.objects.filter(code__in=["super-admin", "admin"]))
    if not roles:
        return

    for module, actions in MODULE_ACTIONS.items():
        for action in actions:
            Permission.objects.get_or_create(module=module, action=action)

    all_permissions = list(Permission.objects.all())
    for role in roles:
        wanted = all_permissions if role.code == "super-admin" else [p for p in all_permissions if p.action != "delete"]
        have = set(RolePermission.objects.filter(role=role).values_list("permission_id", flat=True))
        missing = [RolePermission(role=role, permission=p) for p in wanted if p.pk not in have]
        if missing:
            RolePermission.objects.bulk_create(missing)
