import pytest
from django.urls import reverse

from apps.accounts.models import Permission, RolePermission
from apps.accounts.tests.factories import RoleFactory, UserFactory
from apps.clients.tests.factories import ClientFactory
from apps.locations.tests.factories import SiteFactory
from apps.routes.models import Route
from apps.routes.tests.factories import RouteFactory, RouteStopFactory

pytestmark = pytest.mark.django_db


def _role_with(*module_actions):
    role = RoleFactory()
    for module, action in module_actions:
        permission, _ = Permission.objects.get_or_create(module=module, action=action)
        RolePermission.objects.create(role=role, permission=permission)
    return role


def _formset_management_data(prefix="stops", total=0, initial=0):
    return {
        f"{prefix}-TOTAL_FORMS": str(total),
        f"{prefix}-INITIAL_FORMS": str(initial),
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "1000",
    }


class TestRouteListView:
    def test_anonymous_redirected_to_login(self, client):
        response = client.get(reverse("routes:route_list"))
        assert response.status_code == 302

    def test_user_without_permission_gets_403(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("routes:route_list"))
        assert response.status_code == 403

    def test_user_with_view_permission_sees_list(self, client):
        role = _role_with((Permission.Module.ROUTE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        RouteFactory(name="Depot Circuit")
        client.force_login(viewer)
        response = client.get(reverse("routes:route_list"))
        assert response.status_code == 200
        assert b"Depot Circuit" in response.content

    def test_search_filters_results(self, client):
        role = _role_with((Permission.Module.ROUTE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        RouteFactory(name="Alpha Route")
        RouteFactory(name="Beta Route")
        client.force_login(viewer)
        response = client.get(reverse("routes:route_list"), {"q": "Alpha"})
        assert b"Alpha Route" in response.content
        assert b"Beta Route" not in response.content

    def test_export_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("routes:route_export"))
        assert response.status_code == 403

    def test_export_returns_csv(self, client):
        role = _role_with((Permission.Module.ROUTE, Permission.Action.EXPORT))
        actor = UserFactory(role=role)
        RouteFactory(name="ExportedRoute")
        client.force_login(actor)
        response = client.get(reverse("routes:route_export"))
        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv"
        assert b"ExportedRoute" in response.content


class TestRouteCreateView:
    def test_create_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("routes:route_create"))
        assert response.status_code == 403

    def test_create_route_without_stops(self, client):
        role = _role_with(
            (Permission.Module.ROUTE, Permission.Action.VIEW),
            (Permission.Module.ROUTE, Permission.Action.CREATE),
        )
        actor = UserFactory(role=role)
        client.force_login(actor)
        response = client.post(
            reverse("routes:route_create"),
            {
                "code": "RTNEW01", "name": "New Route", "status": Route.Status.ACTIVE,
                **_formset_management_data(),
            },
        )
        assert response.status_code == 302
        assert Route.objects.filter(code="RTNEW01").exists()

    def test_create_route_with_stops(self, client):
        role = _role_with(
            (Permission.Module.ROUTE, Permission.Action.VIEW),
            (Permission.Module.ROUTE, Permission.Action.CREATE),
        )
        actor = UserFactory(role=role)
        site_a = SiteFactory()
        site_b = SiteFactory()
        client.force_login(actor)
        response = client.post(
            reverse("routes:route_create"),
            {
                "code": "RTSTOPS1", "name": "Route With Stops", "status": Route.Status.ACTIVE,
                **_formset_management_data(total=2),
                "stops-0-site": site_a.pk, "stops-0-sequence": 1, "stops-0-notes": "",
                "stops-1-site": site_b.pk, "stops-1-sequence": 2, "stops-1-notes": "",
            },
        )
        assert response.status_code == 302
        route = Route.objects.get(code="RTSTOPS1")
        assert route.stops.count() == 2

    def test_invalid_stops_formset_rolls_back_route_creation(self, client):
        role = _role_with(
            (Permission.Module.ROUTE, Permission.Action.VIEW),
            (Permission.Module.ROUTE, Permission.Action.CREATE),
        )
        actor = UserFactory(role=role)
        site_a = SiteFactory()
        client.force_login(actor)
        response = client.post(
            reverse("routes:route_create"),
            {
                "code": "RTBAD01", "name": "Bad Route", "status": Route.Status.ACTIVE,
                **_formset_management_data(total=2),
                "stops-0-site": site_a.pk, "stops-0-sequence": 1, "stops-0-notes": "",
                "stops-1-site": site_a.pk, "stops-1-sequence": 1, "stops-1-notes": "",  # duplicate sequence
            },
        )
        assert response.status_code == 200
        assert not Route.objects.filter(code="RTBAD01").exists()


class TestRouteDetailView:
    def test_detail_requires_permission(self, client):
        actor = UserFactory(role=None)
        target = RouteFactory()
        client.force_login(actor)
        response = client.get(reverse("routes:route_detail", kwargs={"uuid": target.uuid}))
        assert response.status_code == 403

    def test_detail_shows_stops_in_order(self, client):
        role = _role_with((Permission.Module.ROUTE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        route = RouteFactory()
        RouteStopFactory(route=route, sequence=2)
        RouteStopFactory(route=route, sequence=1)
        client.force_login(viewer)
        response = client.get(reverse("routes:route_detail", kwargs={"uuid": route.uuid}))
        assert response.status_code == 200
        stops = list(response.context["stops"])
        assert [s.sequence for s in stops] == [1, 2]


class TestRouteLifecycleActions:
    def test_deactivate_route(self, client):
        role = _role_with(
            (Permission.Module.ROUTE, Permission.Action.VIEW),
            (Permission.Module.ROUTE, Permission.Action.ARCHIVE),
        )
        actor = UserFactory(role=role)
        target = RouteFactory(status=Route.Status.ACTIVE)
        client.force_login(actor)
        response = client.post(reverse("routes:route_deactivate", kwargs={"uuid": target.uuid}))
        assert response.status_code == 302
        target.refresh_from_db()
        assert target.status == Route.Status.INACTIVE

    def test_activate_route(self, client):
        role = _role_with(
            (Permission.Module.ROUTE, Permission.Action.VIEW),
            (Permission.Module.ROUTE, Permission.Action.ARCHIVE),
        )
        actor = UserFactory(role=role)
        target = RouteFactory(status=Route.Status.INACTIVE)
        client.force_login(actor)
        response = client.post(reverse("routes:route_activate", kwargs={"uuid": target.uuid}))
        assert response.status_code == 302
        target.refresh_from_db()
        assert target.status == Route.Status.ACTIVE


class TestRouteClientFilter:
    def test_client_filter_narrows_results(self, client):
        role = _role_with((Permission.Module.ROUTE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        target_client = ClientFactory()
        RouteFactory(name="ClientRoute", client=target_client)
        RouteFactory(name="OtherRoute")
        client.force_login(viewer)
        response = client.get(reverse("routes:route_list"), {"client": target_client.pk})
        assert b"ClientRoute" in response.content
        assert b"OtherRoute" not in response.content
