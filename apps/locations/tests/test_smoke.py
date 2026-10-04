"""End-to-end smoke test for the Sites module: create -> edit -> all 4
detail tabs -> deactivate -> export -> dashboard -> integration with the
Client detail page, mirroring apps.clients.tests.test_smoke.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import Permission, RolePermission
from apps.accounts.tests.factories import RoleFactory, UserFactory
from apps.clients.tests.factories import ClientFactory
from apps.locations.models import Site

pytestmark = pytest.mark.django_db


def _full_access_role():
    role = RoleFactory()
    for module in [Permission.Module.SITE, Permission.Module.CLIENT]:
        for action in [
            Permission.Action.VIEW, Permission.Action.CREATE, Permission.Action.UPDATE,
            Permission.Action.ARCHIVE, Permission.Action.EXPORT,
        ]:
            permission, _ = Permission.objects.get_or_create(module=module, action=action)
            RolePermission.objects.create(role=role, permission=permission)
    return role


class TestSiteModuleSmoke:
    def test_full_site_lifecycle_and_client_integration(self, client):
        role = _full_access_role()
        actor = UserFactory(role=role)
        client.force_login(actor)

        target_client = ClientFactory(client_name="Smoke Test Client")

        create_response = client.post(
            reverse("locations:site_create"),
            {
                "site_code": "SITESMOKE1",
                "site_name": "Smoke Test Warehouse",
                "site_type": Site.SiteType.WAREHOUSE,
                "client": target_client.id,
                "status": Site.Status.ACTIVE,
            },
        )
        assert create_response.status_code == 302
        site = Site.objects.get(site_code="SITESMOKE1")

        edit_response = client.get(reverse("locations:site_edit", kwargs={"uuid": site.uuid}))
        assert edit_response.status_code == 200

        for tab in ["overview", "trips", "documents", "history"]:
            url = reverse("locations:site_detail", kwargs={"uuid": site.uuid})
            response = client.get(url, {"tab": tab} if tab != "overview" else {})
            assert response.status_code == 200, f"tab={tab} failed"

        # Sites tab on the client detail page must reflect this real site.
        client_sites_response = client.get(
            reverse("clients:client_detail", kwargs={"uuid": target_client.uuid}), {"tab": "sites"}
        )
        assert client_sites_response.status_code == 200
        assert b"Smoke Test Warehouse" in client_sites_response.content

        # Client overview KPI grid should reflect the real site count too.
        client_overview_response = client.get(reverse("clients:client_detail", kwargs={"uuid": target_client.uuid}))
        assert client_overview_response.status_code == 200

        deactivate_response = client.post(reverse("locations:site_deactivate", kwargs={"uuid": site.uuid}))
        assert deactivate_response.status_code == 302

        list_response = client.get(reverse("locations:site_list"))
        assert list_response.status_code == 200

        export_response = client.get(reverse("locations:site_export"))
        assert export_response.status_code == 200
        assert export_response["Content-Type"] == "text/csv"

        dashboard_response = client.get(reverse("core:dashboard"))
        assert dashboard_response.status_code == 200
        assert b"Total Sites" in dashboard_response.content
