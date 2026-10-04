import pytest

from apps.accounts.tests.factories import UserFactory
from apps.notifications import services
from apps.notifications.models import Notification

pytestmark = pytest.mark.django_db


class TestNotifyService:
    def test_notify_creates_a_notification(self):
        user = UserFactory(role=None)
        notification = services.notify(user, title="Hello", body="World", link_url="/somewhere/")
        assert Notification.objects.filter(pk=notification.pk).exists()
        assert notification.recipient == user
        assert notification.is_read is False

    def test_notify_many_creates_one_per_recipient(self):
        users = UserFactory.create_batch(3, role=None)
        services.notify_many(users, title="Broadcast")
        assert Notification.objects.filter(title="Broadcast").count() == 3

    def test_unread_count_excludes_read_and_archived(self):
        user = UserFactory(role=None)
        services.notify(user, title="Unread")
        read_one = services.notify(user, title="Read")
        read_one.mark_read()
        archived_one = services.notify(user, title="Archived")
        archived_one.is_archived = True
        archived_one.save(update_fields=["is_archived"])
        assert services.unread_count(user) == 1


class TestNotificationModel:
    def test_mark_read_sets_timestamp(self):
        user = UserFactory(role=None)
        notification = services.notify(user, title="Test")
        assert notification.read_at is None
        notification.mark_read()
        assert notification.is_read is True
        assert notification.read_at is not None

    def test_mark_read_is_idempotent(self):
        user = UserFactory(role=None)
        notification = services.notify(user, title="Test")
        notification.mark_read()
        first_read_at = notification.read_at
        notification.mark_read()
        assert notification.read_at == first_read_at
