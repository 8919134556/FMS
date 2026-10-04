import pytest
from django.urls import reverse

from apps.accounts.models import Permission, RolePermission
from apps.accounts.tests.factories import RoleFactory, UserFactory
from apps.alerts.models import Alert
from apps.alerts.tests.factories import AlertFactory
from apps.trips.models import Trip
from apps.trips.tests.factories import TripFactory

pytestmark = pytest.mark.django_db


def _role_with(*module_actions):
    role = RoleFactory()
    for module, action in module_actions:
        permission, _ = Permission.objects.get_or_create(module=module, action=action)
        RolePermission.objects.create(role=role, permission=permission)
    return role


class TestAlertListView:
    def test_anonymous_redirected_to_login(self, client):
        response = client.get(reverse("alerts:alert_list"))
        assert response.status_code == 302

    def test_user_without_permission_gets_403(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("alerts:alert_list"))
        assert response.status_code == 403

    def test_view_syncs_and_shows_real_alert(self, client):
        role = _role_with((Permission.Module.ALERT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        trip = TripFactory(status=Trip.Status.DELAYED)
        client.force_login(viewer)
        response = client.get(reverse("alerts:alert_list"))
        assert response.status_code == 200
        assert trip.trip_number.encode() in response.content

    def test_status_filter(self, client):
        # Every category is sync-managed (see apps.alerts.services), so a
        # hand-crafted Alert with no matching live condition gets swept to
        # RESOLVED on the very next sync. To exercise the filter honestly,
        # drive both states through real conditions instead of AlertFactory.
        role = _role_with((Permission.Module.ALERT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        still_open = TripFactory(status=Trip.Status.DELAYED, trip_number="TRP-STILLOPEN")
        to_resolve = TripFactory(status=Trip.Status.DELAYED, trip_number="TRP-TORESOLVE")
        client.force_login(viewer)
        client.get(reverse("alerts:alert_list"))  # first sync: both become OPEN alerts

        to_resolve.status = Trip.Status.COMPLETED
        to_resolve.save(update_fields=["status"])

        response = client.get(reverse("alerts:alert_list"), {"status": "OPEN"})  # second sync auto-resolves TRP-TORESOLVE
        assert still_open.trip_number.encode() in response.content
        assert to_resolve.trip_number.encode() not in response.content

    def test_archived_alerts_are_hidden(self, client):
        role = _role_with((Permission.Module.ALERT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        AlertFactory(is_archived=True, title="ArchivedOne")
        client.force_login(viewer)
        response = client.get(reverse("alerts:alert_list"))
        assert b"ArchivedOne" not in response.content


class TestAlertActions:
    def test_acknowledge_requires_update_permission(self, client):
        actor = UserFactory(role=None)
        alert = AlertFactory()
        client.force_login(actor)
        response = client.post(reverse("alerts:alert_acknowledge", kwargs={"uuid": alert.uuid}))
        assert response.status_code == 403

    def test_acknowledge_sets_status_and_actor(self, client):
        role = _role_with((Permission.Module.ALERT, Permission.Action.UPDATE))
        actor = UserFactory(role=role)
        alert = AlertFactory(status=Alert.Status.OPEN)
        client.force_login(actor)
        response = client.post(reverse("alerts:alert_acknowledge", kwargs={"uuid": alert.uuid}))
        assert response.status_code == 302
        alert.refresh_from_db()
        assert alert.status == Alert.Status.ACKNOWLEDGED
        assert alert.acknowledged_by == actor

    def test_resolve_sets_status_and_actor(self, client):
        role = _role_with((Permission.Module.ALERT, Permission.Action.UPDATE))
        actor = UserFactory(role=role)
        alert = AlertFactory(status=Alert.Status.OPEN)
        client.force_login(actor)
        response = client.post(reverse("alerts:alert_resolve", kwargs={"uuid": alert.uuid}))
        assert response.status_code == 302
        alert.refresh_from_db()
        assert alert.status == Alert.Status.RESOLVED
        assert alert.resolved_by == actor

    def test_archive_requires_archive_permission(self, client):
        actor = UserFactory(role=None)
        alert = AlertFactory()
        client.force_login(actor)
        response = client.post(reverse("alerts:alert_archive", kwargs={"uuid": alert.uuid}))
        assert response.status_code == 403

    def test_archive_hides_alert(self, client):
        role = _role_with((Permission.Module.ALERT, Permission.Action.ARCHIVE))
        actor = UserFactory(role=role)
        alert = AlertFactory()
        client.force_login(actor)
        response = client.post(reverse("alerts:alert_archive", kwargs={"uuid": alert.uuid}))
        assert response.status_code == 302
        alert.refresh_from_db()
        assert alert.is_archived is True

    def test_actions_write_audit_log(self, client):
        role = _role_with((Permission.Module.ALERT, Permission.Action.UPDATE))
        actor = UserFactory(role=role)
        alert = AlertFactory()
        client.force_login(actor)
        client.post(reverse("alerts:alert_acknowledge", kwargs={"uuid": alert.uuid}))

        from apps.audit.models import AuditLog

        assert AuditLog.objects.filter(module="alert", entity_id=str(alert.pk)).exists()
