import pytest

from apps.audit.models import AuditLog
from apps.audit.services import log_action

pytestmark = pytest.mark.django_db


def test_log_action_creates_entry(admin_user):
    entry = log_action(
        action=AuditLog.Action.CREATE,
        module="user",
        entity="User",
        entity_id=str(admin_user.pk),
        new_value={"username": admin_user.username},
        user=admin_user,
    )
    assert entry.pk is not None
    assert entry.action == AuditLog.Action.CREATE
    assert entry.user == admin_user


def test_audit_log_is_immutable(admin_user):
    entry = log_action(action=AuditLog.Action.CREATE, module="user", entity="User", entity_id="1", user=admin_user)
    entry.module = "changed"
    with pytest.raises(ValueError):
        entry.save()


def test_audit_log_cannot_be_deleted(admin_user):
    entry = log_action(action=AuditLog.Action.CREATE, module="user", entity="User", entity_id="1", user=admin_user)
    with pytest.raises(ValueError):
        entry.delete()


def test_log_action_without_user_stores_null_user():
    entry = log_action(action=AuditLog.Action.LOGIN, module="auth", entity="User", entity_id="0", user=None)
    assert entry.user is None
