import pytest
from django.urls import reverse

from apps.accounts.models import Permission, RolePermission
from apps.accounts.tests.factories import RoleFactory, UserFactory
from apps.geofences.models import Geofence
from apps.geofences.tests.factories import GeofenceFactory

pytestmark = pytest.mark.django_db


def _role_with(*module_actions):
    role = RoleFactory()
    for module, action in module_actions:
        permission, _ = Permission.objects.get_or_create(module=module, action=action)
        RolePermission.objects.create(role=role, permission=permission)
    return role


class TestGeofenceListView:
    def test_anonymous_redirected_to_login(self, client):
        response = client.get(reverse("geofences:geofence_list"))
        assert response.status_code == 302

    def test_user_without_permission_gets_403(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("geofences:geofence_list"))
        assert response.status_code == 403

    def test_user_with_view_permission_sees_list(self, client):
        role = _role_with((Permission.Module.GEOFENCE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        GeofenceFactory(name="Warehouse Zone")
        client.force_login(viewer)
        response = client.get(reverse("geofences:geofence_list"))
        assert response.status_code == 200
        assert b"Warehouse Zone" in response.content

    def test_search_filters_results(self, client):
        role = _role_with((Permission.Module.GEOFENCE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        GeofenceFactory(name="Alpha Zone")
        GeofenceFactory(name="Beta Zone")
        client.force_login(viewer)
        response = client.get(reverse("geofences:geofence_list"), {"q": "Alpha"})
        assert b"Alpha Zone" in response.content
        assert b"Beta Zone" not in response.content

    def test_export_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("geofences:geofence_export"))
        assert response.status_code == 403

    def test_export_returns_csv(self, client):
        role = _role_with((Permission.Module.GEOFENCE, Permission.Action.EXPORT))
        actor = UserFactory(role=role)
        GeofenceFactory(name="ExportedZone")
        client.force_login(actor)
        response = client.get(reverse("geofences:geofence_export"))
        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv"
        assert b"ExportedZone" in response.content


class TestGeofenceCreateView:
    def test_create_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("geofences:geofence_create"))
        assert response.status_code == 403

    def test_create_geofence_success(self, client):
        role = _role_with(
            (Permission.Module.GEOFENCE, Permission.Action.VIEW),
            (Permission.Module.GEOFENCE, Permission.Action.CREATE),
        )
        actor = UserFactory(role=role)
        client.force_login(actor)
        response = client.post(
            reverse("geofences:geofence_create"),
            {
                "code": "GEONEW01", "name": "New Zone", "center_latitude": "12.9716",
                "center_longitude": "77.5946", "radius_meters": 300, "status": Geofence.Status.ACTIVE,
            },
        )
        assert response.status_code == 302
        assert Geofence.objects.filter(code="GEONEW01").exists()

    def test_invalid_latitude_rejected(self, client):
        role = _role_with(
            (Permission.Module.GEOFENCE, Permission.Action.VIEW),
            (Permission.Module.GEOFENCE, Permission.Action.CREATE),
        )
        actor = UserFactory(role=role)
        client.force_login(actor)
        response = client.post(
            reverse("geofences:geofence_create"),
            {
                "code": "GEOBAD01", "name": "Bad Zone", "center_latitude": "200",
                "center_longitude": "77.5946", "radius_meters": 300, "status": Geofence.Status.ACTIVE,
            },
        )
        assert response.status_code == 200
        assert not Geofence.objects.filter(code="GEOBAD01").exists()

    def test_radius_below_minimum_rejected(self, client):
        role = _role_with(
            (Permission.Module.GEOFENCE, Permission.Action.VIEW),
            (Permission.Module.GEOFENCE, Permission.Action.CREATE),
        )
        actor = UserFactory(role=role)
        client.force_login(actor)
        response = client.post(
            reverse("geofences:geofence_create"),
            {
                "code": "GEOSMALL", "name": "Tiny Zone", "center_latitude": "12.9716",
                "center_longitude": "77.5946", "radius_meters": 10, "status": Geofence.Status.ACTIVE,
            },
        )
        assert response.status_code == 200
        assert not Geofence.objects.filter(code="GEOSMALL").exists()


class TestGeofenceDetailView:
    def test_detail_requires_permission(self, client):
        actor = UserFactory(role=None)
        target = GeofenceFactory()
        client.force_login(actor)
        response = client.get(reverse("geofences:geofence_detail", kwargs={"uuid": target.uuid}))
        assert response.status_code == 403

    def test_detail_shows_recent_events(self, client):
        from apps.geofences.tests.factories import GeofenceEventFactory

        role = _role_with((Permission.Module.GEOFENCE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        target = GeofenceFactory()
        GeofenceEventFactory(geofence=target)
        client.force_login(viewer)
        response = client.get(reverse("geofences:geofence_detail", kwargs={"uuid": target.uuid}))
        assert response.status_code == 200
        assert response.context["event_count"] == 1


class TestGeofenceMapDataView:
    def test_requires_geofence_view_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("geofences:geofence_map_data"))
        assert response.status_code == 403

    def test_returns_only_active_geofences(self, client):
        role = _role_with((Permission.Module.GEOFENCE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        GeofenceFactory(name="ActiveZone", status=Geofence.Status.ACTIVE)
        GeofenceFactory(name="InactiveZone", status=Geofence.Status.INACTIVE)
        client.force_login(viewer)
        response = client.get(reverse("geofences:geofence_map_data"))
        assert response.status_code == 200
        data = response.json()
        names = [g["name"] for g in data["results"]]
        assert "ActiveZone" in names
        assert "InactiveZone" not in names


class TestGeofenceLifecycleActions:
    def test_deactivate_geofence(self, client):
        role = _role_with(
            (Permission.Module.GEOFENCE, Permission.Action.VIEW),
            (Permission.Module.GEOFENCE, Permission.Action.ARCHIVE),
        )
        actor = UserFactory(role=role)
        target = GeofenceFactory(status=Geofence.Status.ACTIVE)
        client.force_login(actor)
        response = client.post(reverse("geofences:geofence_deactivate", kwargs={"uuid": target.uuid}))
        assert response.status_code == 302
        target.refresh_from_db()
        assert target.status == Geofence.Status.INACTIVE

    def test_activate_geofence(self, client):
        role = _role_with(
            (Permission.Module.GEOFENCE, Permission.Action.VIEW),
            (Permission.Module.GEOFENCE, Permission.Action.ARCHIVE),
        )
        actor = UserFactory(role=role)
        target = GeofenceFactory(status=Geofence.Status.INACTIVE)
        client.force_login(actor)
        response = client.post(reverse("geofences:geofence_activate", kwargs={"uuid": target.uuid}))
        assert response.status_code == 302
        target.refresh_from_db()
        assert target.status == Geofence.Status.ACTIVE
