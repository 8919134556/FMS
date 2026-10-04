import pytest
from django.urls import reverse

from apps.accounts.models import Permission
from apps.accounts.tests.factories import RoleFactory, UserFactory
from apps.notifications.models import Notification
from apps.notifications.tests.factories import NotificationFactory

pytestmark = pytest.mark.django_db


def _role_with(*module_actions):
    from apps.accounts.models import RolePermission

    role = RoleFactory()
    for module, action in module_actions:
        permission, _ = Permission.objects.get_or_create(module=module, action=action)
        RolePermission.objects.create(role=role, permission=permission)
    return role


class TestNotificationListView:
    def test_anonymous_redirected_to_login(self, client):
        response = client.get(reverse("notifications:notification_list"))
        assert response.status_code == 302

    def test_any_authenticated_user_sees_own_inbox_without_module_permission(self, client):
        viewer = UserFactory(role=None)
        NotificationFactory(recipient=viewer, title="Yours")
        client.force_login(viewer)
        response = client.get(reverse("notifications:notification_list"))
        assert response.status_code == 200
        assert b"Yours" in response.content

    def test_cannot_see_other_users_notifications(self, client):
        viewer = UserFactory(role=None)
        other = UserFactory(role=None)
        NotificationFactory(recipient=other, title="NotYours")
        client.force_login(viewer)
        response = client.get(reverse("notifications:notification_list"))
        assert b"NotYours" not in response.content

    def test_unread_filter(self, client):
        viewer = UserFactory(role=None)
        NotificationFactory(recipient=viewer, title="UnreadOne", is_read=False)
        NotificationFactory(recipient=viewer, title="ReadOne", is_read=True)
        client.force_login(viewer)
        response = client.get(reverse("notifications:notification_list"), {"status": "unread"})
        assert b"UnreadOne" in response.content
        assert b"ReadOne" not in response.content

    def test_archived_notifications_are_hidden(self, client):
        viewer = UserFactory(role=None)
        NotificationFactory(recipient=viewer, title="Archived", is_archived=True)
        client.force_login(viewer)
        response = client.get(reverse("notifications:notification_list"))
        assert b"Archived" not in response.content


class TestNotificationFeedView:
    def test_feed_returns_unread_count_and_results(self, client):
        viewer = UserFactory(role=None)
        NotificationFactory(recipient=viewer, title="Feed Item", is_read=False)
        client.force_login(viewer)
        response = client.get(reverse("notifications:notification_feed"))
        assert response.status_code == 200
        data = response.json()
        assert data["unread_count"] == 1
        assert data["results"][0]["title"] == "Feed Item"


class TestNotificationActions:
    def test_mark_read_only_affects_own_notification(self, client):
        viewer = UserFactory(role=None)
        n = NotificationFactory(recipient=viewer, is_read=False)
        client.force_login(viewer)
        response = client.post(reverse("notifications:notification_mark_read", kwargs={"uuid": n.uuid}))
        assert response.status_code == 302
        n.refresh_from_db()
        assert n.is_read is True

    def test_mark_read_404s_for_other_users_notification(self, client):
        viewer = UserFactory(role=None)
        other = UserFactory(role=None)
        n = NotificationFactory(recipient=other, is_read=False)
        client.force_login(viewer)
        response = client.post(reverse("notifications:notification_mark_read", kwargs={"uuid": n.uuid}))
        assert response.status_code == 404
        n.refresh_from_db()
        assert n.is_read is False

    def test_mark_all_read(self, client):
        viewer = UserFactory(role=None)
        NotificationFactory.create_batch(3, recipient=viewer, is_read=False)
        client.force_login(viewer)
        response = client.post(reverse("notifications:notification_mark_all_read"))
        assert response.status_code == 302
        assert Notification.objects.filter(recipient=viewer, is_read=False).count() == 0

    def test_archive_hides_from_default_list(self, client):
        viewer = UserFactory(role=None)
        n = NotificationFactory(recipient=viewer)
        client.force_login(viewer)
        client.post(reverse("notifications:notification_archive", kwargs={"uuid": n.uuid}))
        n.refresh_from_db()
        assert n.is_archived is True


class TestBroadcastNotificationView:
    def test_requires_notification_create_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("notifications:broadcast"))
        assert response.status_code == 403

    def test_broadcast_to_all_users(self, client):
        role = _role_with((Permission.Module.NOTIFICATION, Permission.Action.CREATE))
        actor = UserFactory(role=role)
        others = UserFactory.create_batch(2, role=None)
        client.force_login(actor)
        response = client.post(
            reverse("notifications:broadcast"),
            {"audience": "ALL_USERS", "title": "Scheduled downtime", "body": "Tonight 10pm", "level": "WARNING", "link_url": ""},
        )
        assert response.status_code == 302
        for user in [*others, actor]:
            assert Notification.objects.filter(recipient=user, title="Scheduled downtime").exists()

    def test_broadcast_to_specific_role_only_notifies_that_role(self, client):
        actor_role = _role_with((Permission.Module.NOTIFICATION, Permission.Action.CREATE))
        actor = UserFactory(role=actor_role)
        target_role = RoleFactory()
        targeted_user = UserFactory(role=target_role)
        untargeted_user = UserFactory(role=None)
        client.force_login(actor)
        response = client.post(
            reverse("notifications:broadcast"),
            {"audience": "SPECIFIC_ROLE", "role": target_role.pk, "title": "Role-only message", "level": "INFO", "link_url": ""},
        )
        assert response.status_code == 302
        assert Notification.objects.filter(recipient=targeted_user, title="Role-only message").exists()
        assert not Notification.objects.filter(recipient=untargeted_user, title="Role-only message").exists()

    def test_specific_role_audience_without_role_shows_validation_error(self, client):
        role = _role_with((Permission.Module.NOTIFICATION, Permission.Action.CREATE))
        actor = UserFactory(role=role)
        client.force_login(actor)
        response = client.post(
            reverse("notifications:broadcast"),
            {"audience": "SPECIFIC_ROLE", "title": "Missing role", "level": "INFO", "link_url": ""},
        )
        assert response.status_code == 200
        assert not Notification.objects.filter(title="Missing role").exists()

    def test_broadcast_writes_audit_log(self, client):
        role = _role_with((Permission.Module.NOTIFICATION, Permission.Action.CREATE))
        actor = UserFactory(role=role)
        client.force_login(actor)
        client.post(
            reverse("notifications:broadcast"),
            {"audience": "ALL_USERS", "title": "Audited message", "level": "INFO", "link_url": ""},
        )
        from apps.audit.models import AuditLog

        assert AuditLog.objects.filter(module="notification", action=AuditLog.Action.CREATE).exists()


class TestNotificationHardening:
    def test_anonymous_post_redirects_to_login_not_500(self, client):
        n = NotificationFactory()
        for name, kwargs in [
            ("notifications:notification_mark_read", {"uuid": n.uuid}),
            ("notifications:notification_archive", {"uuid": n.uuid}),
            ("notifications:notification_mark_all_read", {}),
        ]:
            response = client.post(reverse(name, kwargs=kwargs))
            assert response.status_code == 302
            assert "/accounts/login/" in response["Location"]

    def test_broadcast_rejects_javascript_link(self, client):
        role = _role_with((Permission.Module.NOTIFICATION, Permission.Action.CREATE))
        actor = UserFactory(role=role)
        client.force_login(actor)
        response = client.post(
            reverse("notifications:broadcast"),
            {"audience": "ALL_USERS", "title": "Bad link", "level": "INFO", "link_url": "javascript:alert(1)"},
        )
        assert response.status_code == 200
        assert not Notification.objects.filter(title="Bad link").exists()
