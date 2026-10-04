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
                "geofence_type": "ENTRY", "shape": "CIRCLE",
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
                "geofence_type": "ENTRY", "shape": "CIRCLE",
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
                "geofence_type": "ENTRY", "shape": "CIRCLE",
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


class TestGeofenceTypesForm:
    """Entry / Exit / Speed Limit, circle or polygon, assigned vehicles."""

    def _login(self, client, **kwargs):
        role = _role_with(
            (Permission.Module.GEOFENCE, Permission.Action.VIEW),
            (Permission.Module.GEOFENCE, Permission.Action.CREATE),
            (Permission.Module.GEOFENCE, Permission.Action.UPDATE),
        )
        user = UserFactory(role=role, **kwargs)
        client.force_login(user)
        return user

    def _post(self, client, **overrides):
        data = {"code": "GEOT01", "name": "Bangalore City", "status": Geofence.Status.ACTIVE,
                "geofence_type": "ENTRY", "shape": "CIRCLE", "center_latitude": "12.9716",
                "center_longitude": "77.5946", "radius_meters": 3000}
        data.update(overrides)
        return client.post(reverse("geofences:geofence_create"), data)

    def test_speed_limit_geofence_requires_a_valid_limit(self, client):
        self._login(client)
        for bad in ("", "0", "-5", "abc", "301"):
            response = self._post(client, geofence_type="SPEED_LIMIT", speed_limit_kmh=bad)
            assert response.status_code == 200, bad
            assert "speed_limit_kmh" in response.context["form"].errors, bad
        assert self._post(client, geofence_type="SPEED_LIMIT", speed_limit_kmh="40").status_code == 302
        geofence = Geofence.objects.get(code="GEOT01")
        assert (geofence.geofence_type, geofence.speed_limit_kmh) == ("SPEED_LIMIT", 40)

    def test_entry_and_exit_geofences_store_no_speed_limit(self, client):
        self._login(client)
        assert self._post(client, geofence_type="EXIT", speed_limit_kmh="40").status_code == 302
        assert Geofence.objects.get(code="GEOT01").speed_limit_kmh is None

    def test_polygon_from_the_map_editor(self, client):
        self._login(client)
        square = '[[12.97,77.59],[12.97,77.60],[12.98,77.60],[12.98,77.59]]'
        response = self._post(client, shape="POLYGON", polygon_points=square, center_latitude="",
                              center_longitude="", radius_meters="")
        assert response.status_code == 302
        geofence = Geofence.objects.get(code="GEOT01")
        assert geofence.shape == "POLYGON" and len(geofence.polygon) == 4
        assert abs(float(geofence.center_latitude) - 12.975) < 1e-6 and 700 < geofence.radius_meters < 800

    def test_polygon_needs_three_points(self, client):
        self._login(client)
        response = self._post(client, shape="POLYGON", polygon_points="[[12.97,77.59],[12.98,77.60]]")
        assert response.status_code == 200 and "at least 3 points" in str(response.context["form"].non_field_errors())

    def test_circle_needs_a_center(self, client):
        self._login(client)
        response = self._post(client, center_latitude="", center_longitude="")
        assert response.status_code == 200 and "center_latitude" in response.context["form"].errors

    def test_assigned_vehicles_are_saved_and_shown(self, client):
        from apps.vehicles.tests.factories import VehicleFactory

        self._login(client)
        v1, v2 = VehicleFactory(registration_number="KA01AB1234"), VehicleFactory()
        assert self._post(client, vehicles=[v1.pk]).status_code == 302
        geofence = Geofence.objects.get(code="GEOT01")
        assert list(geofence.vehicles.all()) == [v1]
        html = client.get(reverse("geofences:geofence_detail", kwargs={"uuid": geofence.uuid})).content.decode()
        assert "KA01AB1234" in html and "Entry" in html and "gfDetailMap" in html
        assert v2.registration_number not in html

    def test_vehicle_choices_are_scoped_to_the_user(self, client):
        from apps.clients.tests.factories import ClientFactory
        from apps.vehicles.tests.factories import VehicleFactory

        a, b = ClientFactory(), ClientFactory()
        mine, theirs = VehicleFactory(client=a), VehicleFactory(client=b)
        form_cls = __import__("apps.geofences.forms", fromlist=["GeofenceForm"]).GeofenceForm
        scoped_user = UserFactory(client=a)
        choices = list(form_cls(user=scoped_user).fields["vehicles"].queryset)
        assert mine in choices and theirs not in choices

    def test_list_shows_type_and_filters_by_it(self, client):
        GeofenceFactory(name="Speed Zone", geofence_type="SPEED_LIMIT", speed_limit_kmh=40)
        GeofenceFactory(name="Exit Zone", geofence_type="EXIT")
        self._login(client)
        html = client.get(reverse("geofences:geofence_list"), {"type": "SPEED_LIMIT"}).content.decode()
        assert "Speed Zone" in html and "Speed Limit · 40 km/h" in html and "Exit Zone" not in html

    def test_map_data_includes_shape_and_type(self, client):
        GeofenceFactory(name="Poly", shape="POLYGON", polygon=[[12.97, 77.59], [12.97, 77.6], [12.98, 77.6]],
                        geofence_type="EXIT")
        self._login(client)
        [item] = client.get(reverse("geofences:geofence_map_data")).json()["results"]
        assert (item["shape"], item["type"], len(item["polygon"])) == ("POLYGON", "EXIT", 3)


class TestLiveTrackingGeofenceLayer:
    """The Live Tracking map's geofence layer: who sees which geofences."""

    def _square(self):
        return [[12.97, 77.59], [12.97, 77.6], [12.98, 77.6]]

    def test_live_tracking_viewers_get_active_geofences_with_type_details(self, client):
        from apps.vehicles.tests.factories import VehicleFactory

        v1, v2 = VehicleFactory(), VehicleFactory()
        zone = GeofenceFactory(name="School Zone", geofence_type="SPEED_LIMIT", speed_limit_kmh=30, shape="POLYGON",
                               polygon=self._square())
        zone.vehicles.set([v1, v2])
        GeofenceFactory(name="Off Zone", status=Geofence.Status.INACTIVE)
        client.force_login(UserFactory(role=_role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW))))
        [item] = client.get(reverse("geofences:geofence_map_data")).json()["results"]
        assert (item["name"], item["type"], item["type_name"], item["speed_limit_kmh"]) == (
            "School Zone", "SPEED_LIMIT", "Speed Limit", 30)
        assert (item["status"], item["assigned_vehicles"], item["shape"]) == ("Active", 2, "POLYGON")

    def test_client_users_only_get_geofences_of_their_own_vehicles(self, client):
        from apps.clients.tests.factories import ClientFactory
        from apps.vehicles.tests.factories import VehicleFactory

        a, b = ClientFactory(), ClientFactory()
        va, vb = VehicleFactory(client=a), VehicleFactory(client=b)
        shared = GeofenceFactory(name="Shared Zone")
        shared.vehicles.set([va, vb])
        GeofenceFactory(name="Bravo Only").vehicles.set([vb])
        GeofenceFactory(name="Unassigned Zone")
        user = UserFactory(role=_role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW)), client=a)
        client.force_login(user)
        results = client.get(reverse("geofences:geofence_map_data")).json()["results"]
        assert [(g["name"], g["assigned_vehicles"]) for g in results] == [("Shared Zone", 1)]  # only their vehicle

    def test_live_page_has_the_toggle_and_legend(self, client):
        client.force_login(UserFactory(role=_role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW))))
        html = client.get(reverse("tracking:live_map")).content.decode()
        assert 'id="liveGeofenceToggle"' in html and 'aria-pressed="true"' in html
        assert 'id="liveGeofenceLegend"' in html
        for kind in ("ENTRY", "EXIT", "SPEED_LIMIT"):
            assert f'data-geofence-count="{kind}"' in html
