import pytest
from django.urls import reverse

from apps.accounts.models import Permission, RolePermission
from apps.accounts.tests.factories import RoleFactory, UserFactory
from apps.clients.models import Client
from apps.clients.tests.factories import ClientFactory
from apps.drivers.tests.factories import DriverFactory
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


def _role_with(*module_actions):
    role = RoleFactory()
    for module, action in module_actions:
        permission, _ = Permission.objects.get_or_create(module=module, action=action)
        RolePermission.objects.create(role=role, permission=permission)
    return role


class TestClientListView:
    def test_anonymous_redirected_to_login(self, client):
        response = client.get(reverse("clients:client_list"))
        assert response.status_code == 302

    def test_user_without_permission_gets_403(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("clients:client_list"))
        assert response.status_code == 403

    def test_user_with_view_permission_sees_list(self, client):
        role = _role_with((Permission.Module.CLIENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        ClientFactory.create_batch(2)
        client.force_login(viewer)
        response = client.get(reverse("clients:client_list"))
        assert response.status_code == 200
        assert b"Clients" in response.content

    def test_search_filters_results(self, client):
        role = _role_with((Permission.Module.CLIENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        ClientFactory(client_name="Zenith Freight")
        client.force_login(viewer)
        response = client.get(reverse("clients:client_list"), {"q": "Zenith"})
        assert response.status_code == 200
        assert b"Zenith Freight" in response.content

    def test_status_filter(self, client):
        role = _role_with((Permission.Module.CLIENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        ClientFactory(client_name="SuspendedCo", status=Client.Status.SUSPENDED)
        ClientFactory(client_name="ActiveCo", status=Client.Status.ACTIVE)
        client.force_login(viewer)
        response = client.get(reverse("clients:client_list"), {"status": Client.Status.SUSPENDED})
        assert b"SuspendedCo" in response.content
        assert b"ActiveCo" not in response.content

    def test_list_shows_vehicle_and_driver_counts(self, client):
        role = _role_with((Permission.Module.CLIENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        target = ClientFactory(client_name="CountedCo")
        VehicleFactory(client=target)
        DriverFactory(client=target)
        client.force_login(viewer)
        response = client.get(reverse("clients:client_list"))
        assert response.status_code == 200

    def test_export_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("clients:client_export"))
        assert response.status_code == 403

    def test_export_returns_csv(self, client):
        role = _role_with((Permission.Module.CLIENT, Permission.Action.EXPORT))
        actor = UserFactory(role=role)
        ClientFactory(client_name="ExportedClient")
        client.force_login(actor)
        response = client.get(reverse("clients:client_export"))
        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv"
        assert b"ExportedClient" in response.content


class TestClientListQueryPerformance:
    def test_list_query_count_does_not_grow_with_row_count(self, client, django_assert_max_num_queries):
        role = _role_with((Permission.Module.CLIENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        for _ in range(15):
            target = ClientFactory()
            VehicleFactory(client=target)
            DriverFactory(client=target)
        client.force_login(viewer)

        with django_assert_max_num_queries(10):
            response = client.get(reverse("clients:client_list"))
        assert response.status_code == 200


class TestClientCreateView:
    def test_create_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("clients:client_create"))
        assert response.status_code == 403

    def test_create_client_success(self, client):
        role = _role_with(
            (Permission.Module.CLIENT, Permission.Action.VIEW),
            (Permission.Module.CLIENT, Permission.Action.CREATE),
        )
        actor = UserFactory(role=role)
        client.force_login(actor)
        response = client.post(
            reverse("clients:client_create"),
            {
                "client_code": "CLINEW01",
                "client_name": "New Client Co",
                "client_type": Client.ClientType.CORPORATE,
                "status": Client.Status.ACTIVE,
            },
        )
        assert response.status_code == 302
        assert Client.objects.filter(client_code="CLINEW01").exists()

    def test_create_duplicate_code_shows_validation_error(self, client):
        role = _role_with(
            (Permission.Module.CLIENT, Permission.Action.VIEW),
            (Permission.Module.CLIENT, Permission.Action.CREATE),
        )
        actor = UserFactory(role=role)
        ClientFactory(client_code="EXISTING01")
        client.force_login(actor)
        response = client.post(
            reverse("clients:client_create"),
            {
                "client_code": "EXISTING01",
                "client_name": "Duplicate Attempt",
                "client_type": Client.ClientType.CORPORATE,
                "status": Client.Status.ACTIVE,
            },
        )
        assert response.status_code == 200
        assert Client.objects.filter(client_code="EXISTING01").count() == 1


class TestClientDetailView:
    def test_detail_requires_permission(self, client):
        actor = UserFactory(role=None)
        target = ClientFactory()
        client.force_login(actor)
        response = client.get(reverse("clients:client_detail", kwargs={"uuid": target.uuid}))
        assert response.status_code == 403

    def test_all_tabs_render(self, client):
        role = _role_with((Permission.Module.CLIENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        target = ClientFactory(client_name="TabTestClient")
        client.force_login(viewer)
        for tab in ["overview", "vehicles", "drivers", "branches", "contracts", "sites", "documents", "history"]:
            url = reverse("clients:client_detail", kwargs={"uuid": target.uuid})
            response = client.get(url, {"tab": tab} if tab != "overview" else {})
            assert response.status_code == 200, f"tab={tab} failed"

    def test_vehicles_tab_shows_allocated_vehicle(self, client):
        role = _role_with(
            (Permission.Module.CLIENT, Permission.Action.VIEW),
            (Permission.Module.VEHICLE, Permission.Action.VIEW),
        )
        viewer = UserFactory(role=role)
        target = ClientFactory()
        VehicleFactory(client=target, registration_number="KA07ZZ0007")
        client.force_login(viewer)
        response = client.get(reverse("clients:client_detail", kwargs={"uuid": target.uuid}), {"tab": "vehicles"})
        assert b"KA07ZZ0007" in response.content


class TestClientLifecycleActions:
    def test_deactivate_client(self, client):
        role = _role_with(
            (Permission.Module.CLIENT, Permission.Action.VIEW),
            (Permission.Module.CLIENT, Permission.Action.ARCHIVE),
        )
        actor = UserFactory(role=role)
        target = ClientFactory(status=Client.Status.ACTIVE)
        client.force_login(actor)
        response = client.post(reverse("clients:client_deactivate", kwargs={"uuid": target.uuid}))
        assert response.status_code == 302
        target.refresh_from_db()
        assert target.status == Client.Status.INACTIVE

    def test_deactivate_get_not_allowed(self, client):
        role = _role_with((Permission.Module.CLIENT, Permission.Action.ARCHIVE))
        actor = UserFactory(role=role)
        target = ClientFactory()
        client.force_login(actor)
        response = client.get(reverse("clients:client_deactivate", kwargs={"uuid": target.uuid}))
        assert response.status_code == 405

    def test_activate_client(self, client):
        role = _role_with(
            (Permission.Module.CLIENT, Permission.Action.VIEW),
            (Permission.Module.CLIENT, Permission.Action.ARCHIVE),
        )
        actor = UserFactory(role=role)
        target = ClientFactory(status=Client.Status.INACTIVE)
        client.force_login(actor)
        response = client.post(reverse("clients:client_activate", kwargs={"uuid": target.uuid}))
        assert response.status_code == 302
        target.refresh_from_db()
        assert target.status == Client.Status.ACTIVE
