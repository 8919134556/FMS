"""End-to-end smoke test for the Clients module: create -> edit -> all 8
detail tabs -> deactivate -> export -> dashboard, mirroring
apps.vehicles.tests.test_smoke / apps.drivers.tests.test_smoke.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import Permission, RolePermission
from apps.accounts.tests.factories import RoleFactory, UserFactory
from apps.clients.models import Client
from apps.drivers.tests.factories import DriverFactory
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


def _full_access_role():
    role = RoleFactory()
    for module in [Permission.Module.CLIENT, Permission.Module.VEHICLE, Permission.Module.DRIVER]:
        for action in [
            Permission.Action.VIEW, Permission.Action.CREATE, Permission.Action.UPDATE,
            Permission.Action.ARCHIVE, Permission.Action.EXPORT,
        ]:
            permission, _ = Permission.objects.get_or_create(module=module, action=action)
            RolePermission.objects.create(role=role, permission=permission)
    return role


class TestClientModuleSmoke:
    def test_full_client_lifecycle_and_all_detail_tabs_render(self, client):
        role = _full_access_role()
        actor = UserFactory(role=role)
        client.force_login(actor)

        create_response = client.post(
            reverse("clients:client_create"),
            {
                "client_code": "CLISMOKE1",
                "client_name": "Smoke Test Logistics",
                "client_type": Client.ClientType.CORPORATE,
                "status": Client.Status.ACTIVE,
            },
        )
        assert create_response.status_code == 302
        target = Client.objects.get(client_code="CLISMOKE1")

        edit_response = client.get(reverse("clients:client_edit", kwargs={"uuid": target.uuid}))
        assert edit_response.status_code == 200

        VehicleFactory(client=target)
        DriverFactory(client=target)

        for tab in ["overview", "vehicles", "drivers", "branches", "contracts", "sites", "documents", "history"]:
            url = reverse("clients:client_detail", kwargs={"uuid": target.uuid})
            response = client.get(url, {"tab": tab} if tab != "overview" else {})
            assert response.status_code == 200, f"tab={tab} failed"

        overview_response = client.get(reverse("clients:client_detail", kwargs={"uuid": target.uuid}))
        assert b"Smoke Test Logistics" in overview_response.content

        deactivate_response = client.post(reverse("clients:client_deactivate", kwargs={"uuid": target.uuid}))
        assert deactivate_response.status_code == 302

        list_response = client.get(reverse("clients:client_list"))
        assert list_response.status_code == 200

        export_response = client.get(reverse("clients:client_export"))
        assert export_response.status_code == 200
        assert export_response["Content-Type"] == "text/csv"

        dashboard_response = client.get(reverse("core:dashboard"))
        assert dashboard_response.status_code == 200
        assert b"Total Clients" in dashboard_response.content
