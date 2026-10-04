import pytest
from django.urls import reverse

from apps.accounts.models import Permission, RolePermission
from apps.accounts.tests.factories import RoleFactory, UserFactory
from apps.clients.tests.factories import ClientFactory
from apps.locations.models import Branch, Site
from apps.locations.tests.factories import BranchFactory, SiteFactory

pytestmark = pytest.mark.django_db


def _role_with(*module_actions):
    role = RoleFactory()
    for module, action in module_actions:
        permission, _ = Permission.objects.get_or_create(module=module, action=action)
        RolePermission.objects.create(role=role, permission=permission)
    return role


class TestSiteListView:
    def test_anonymous_redirected_to_login(self, client):
        response = client.get(reverse("locations:site_list"))
        assert response.status_code == 302

    def test_user_without_permission_gets_403(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("locations:site_list"))
        assert response.status_code == 403

    def test_user_with_view_permission_sees_list(self, client):
        role = _role_with((Permission.Module.SITE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        SiteFactory.create_batch(2)
        client.force_login(viewer)
        response = client.get(reverse("locations:site_list"))
        assert response.status_code == 200
        assert b"Sites" in response.content

    def test_search_filters_results(self, client):
        role = _role_with((Permission.Module.SITE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        SiteFactory(site_name="Northgate Warehouse")
        client.force_login(viewer)
        response = client.get(reverse("locations:site_list"), {"q": "Northgate"})
        assert response.status_code == 200
        assert b"Northgate Warehouse" in response.content

    def test_client_filter(self, client):
        role = _role_with((Permission.Module.SITE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        target_client = ClientFactory(client_name="Filtered Client")
        SiteFactory(site_name="ClientSite", client=target_client)
        SiteFactory(site_name="OtherSite")
        client.force_login(viewer)
        response = client.get(reverse("locations:site_list"), {"client": target_client.id})
        assert b"ClientSite" in response.content
        assert b"OtherSite" not in response.content

    def test_export_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("locations:site_export"))
        assert response.status_code == 403

    def test_export_returns_csv(self, client):
        role = _role_with((Permission.Module.SITE, Permission.Action.EXPORT))
        actor = UserFactory(role=role)
        SiteFactory(site_name="ExportedSite")
        client.force_login(actor)
        response = client.get(reverse("locations:site_export"))
        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv"
        assert b"ExportedSite" in response.content


class TestSiteListQueryPerformance:
    def test_list_query_count_does_not_grow_with_row_count(self, client, django_assert_max_num_queries):
        role = _role_with((Permission.Module.SITE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        for _ in range(15):
            SiteFactory(branch=BranchFactory())
        client.force_login(viewer)

        with django_assert_max_num_queries(10):
            response = client.get(reverse("locations:site_list"))
        assert response.status_code == 200


class TestSiteCreateView:
    def test_create_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("locations:site_create"))
        assert response.status_code == 403

    def test_create_site_success(self, client):
        role = _role_with(
            (Permission.Module.SITE, Permission.Action.VIEW),
            (Permission.Module.SITE, Permission.Action.CREATE),
        )
        actor = UserFactory(role=role)
        target_client = ClientFactory()
        client.force_login(actor)
        response = client.post(
            reverse("locations:site_create"),
            {
                "site_code": "SITENEW01",
                "site_name": "New Warehouse",
                "site_type": Site.SiteType.WAREHOUSE,
                "client": target_client.id,
                "status": Site.Status.ACTIVE,
            },
        )
        assert response.status_code == 302
        assert Site.objects.filter(site_code="SITENEW01").exists()

    def test_create_without_client_rejected(self, client):
        role = _role_with(
            (Permission.Module.SITE, Permission.Action.VIEW),
            (Permission.Module.SITE, Permission.Action.CREATE),
        )
        actor = UserFactory(role=role)
        client.force_login(actor)
        response = client.post(
            reverse("locations:site_create"),
            {
                "site_code": "SITENOCLIENT",
                "site_name": "No Client Site",
                "site_type": Site.SiteType.WAREHOUSE,
                "status": Site.Status.ACTIVE,
            },
        )
        assert response.status_code == 200
        assert not Site.objects.filter(site_code="SITENOCLIENT").exists()


class TestSiteDetailView:
    def test_detail_requires_permission(self, client):
        actor = UserFactory(role=None)
        target = SiteFactory()
        client.force_login(actor)
        response = client.get(reverse("locations:site_detail", kwargs={"uuid": target.uuid}))
        assert response.status_code == 403

    def test_all_tabs_render(self, client):
        role = _role_with((Permission.Module.SITE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        target = SiteFactory(site_name="TabTestSite")
        client.force_login(viewer)
        for tab in ["overview", "trips", "documents", "history"]:
            url = reverse("locations:site_detail", kwargs={"uuid": target.uuid})
            response = client.get(url, {"tab": tab} if tab != "overview" else {})
            assert response.status_code == 200, f"tab={tab} failed"


class TestSiteLifecycleActions:
    def test_deactivate_site(self, client):
        role = _role_with(
            (Permission.Module.SITE, Permission.Action.VIEW),
            (Permission.Module.SITE, Permission.Action.ARCHIVE),
        )
        actor = UserFactory(role=role)
        target = SiteFactory(status=Site.Status.ACTIVE)
        client.force_login(actor)
        response = client.post(reverse("locations:site_deactivate", kwargs={"uuid": target.uuid}))
        assert response.status_code == 302
        target.refresh_from_db()
        assert target.status == Site.Status.INACTIVE

    def test_deactivate_get_not_allowed(self, client):
        role = _role_with((Permission.Module.SITE, Permission.Action.ARCHIVE))
        actor = UserFactory(role=role)
        target = SiteFactory()
        client.force_login(actor)
        response = client.get(reverse("locations:site_deactivate", kwargs={"uuid": target.uuid}))
        assert response.status_code == 405

    def test_activate_site(self, client):
        role = _role_with(
            (Permission.Module.SITE, Permission.Action.VIEW),
            (Permission.Module.SITE, Permission.Action.ARCHIVE),
        )
        actor = UserFactory(role=role)
        target = SiteFactory(status=Site.Status.INACTIVE)
        client.force_login(actor)
        response = client.post(reverse("locations:site_activate", kwargs={"uuid": target.uuid}))
        assert response.status_code == 302
        target.refresh_from_db()
        assert target.status == Site.Status.ACTIVE


class TestBranchListView:
    def test_anonymous_redirected_to_login(self, client):
        response = client.get(reverse("locations:branch_list"))
        assert response.status_code == 302

    def test_user_without_permission_gets_403(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("locations:branch_list"))
        assert response.status_code == 403

    def test_user_with_view_permission_sees_list(self, client):
        role = _role_with((Permission.Module.BRANCH, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        BranchFactory.create_batch(2)
        client.force_login(viewer)
        response = client.get(reverse("locations:branch_list"))
        assert response.status_code == 200
        assert b"Branches" in response.content

    def test_search_filters_results(self, client):
        role = _role_with((Permission.Module.BRANCH, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        BranchFactory(name="Northgate Depot")
        client.force_login(viewer)
        response = client.get(reverse("locations:branch_list"), {"q": "Northgate"})
        assert response.status_code == 200
        assert b"Northgate Depot" in response.content

    def test_export_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("locations:branch_export"))
        assert response.status_code == 403

    def test_export_returns_csv(self, client):
        role = _role_with((Permission.Module.BRANCH, Permission.Action.EXPORT))
        actor = UserFactory(role=role)
        BranchFactory(name="ExportedBranch")
        client.force_login(actor)
        response = client.get(reverse("locations:branch_export"))
        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv"
        assert b"ExportedBranch" in response.content


class TestBranchListQueryPerformance:
    def test_list_query_count_does_not_grow_with_row_count(self, client, django_assert_max_num_queries):
        role = _role_with((Permission.Module.BRANCH, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        for _ in range(15):
            BranchFactory(client=ClientFactory())
        client.force_login(viewer)

        with django_assert_max_num_queries(10):
            response = client.get(reverse("locations:branch_list"))
        assert response.status_code == 200


class TestBranchCreateView:
    def test_create_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("locations:branch_create"))
        assert response.status_code == 403

    def test_create_branch_success(self, client):
        role = _role_with(
            (Permission.Module.BRANCH, Permission.Action.VIEW),
            (Permission.Module.BRANCH, Permission.Action.CREATE),
        )
        actor = UserFactory(role=role)
        client.force_login(actor)
        response = client.post(
            reverse("locations:branch_create"),
            {
                "code": "BRNEW01", "name": "New Depot",
                "branch_type": Branch.BranchType.DEPOT, "status": Branch.Status.ACTIVE,
            },
        )
        assert response.status_code == 302
        assert Branch.objects.filter(code="BRNEW01").exists()

    def test_create_without_name_rejected(self, client):
        role = _role_with(
            (Permission.Module.BRANCH, Permission.Action.VIEW),
            (Permission.Module.BRANCH, Permission.Action.CREATE),
        )
        actor = UserFactory(role=role)
        client.force_login(actor)
        response = client.post(
            reverse("locations:branch_create"),
            {"code": "BRNONAME", "branch_type": Branch.BranchType.DEPOT, "status": Branch.Status.ACTIVE},
        )
        assert response.status_code == 200
        assert not Branch.objects.filter(code="BRNONAME").exists()


class TestBranchDetailView:
    def test_detail_requires_permission(self, client):
        actor = UserFactory(role=None)
        target = BranchFactory()
        client.force_login(actor)
        response = client.get(reverse("locations:branch_detail", kwargs={"uuid": target.uuid}))
        assert response.status_code == 403

    def test_all_tabs_render(self, client):
        role = _role_with((Permission.Module.BRANCH, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        target = BranchFactory(name="TabTestBranch")
        client.force_login(viewer)
        for tab in ["overview", "sites", "vehicles", "history"]:
            url = reverse("locations:branch_detail", kwargs={"uuid": target.uuid})
            response = client.get(url, {"tab": tab} if tab != "overview" else {})
            assert response.status_code == 200, f"tab={tab} failed"


class TestBranchLifecycleActions:
    def test_deactivate_branch(self, client):
        role = _role_with(
            (Permission.Module.BRANCH, Permission.Action.VIEW),
            (Permission.Module.BRANCH, Permission.Action.ARCHIVE),
        )
        actor = UserFactory(role=role)
        target = BranchFactory(status=Branch.Status.ACTIVE)
        client.force_login(actor)
        response = client.post(reverse("locations:branch_deactivate", kwargs={"uuid": target.uuid}))
        assert response.status_code == 302
        target.refresh_from_db()
        assert target.status == Branch.Status.INACTIVE

    def test_activate_branch(self, client):
        role = _role_with(
            (Permission.Module.BRANCH, Permission.Action.VIEW),
            (Permission.Module.BRANCH, Permission.Action.ARCHIVE),
        )
        actor = UserFactory(role=role)
        target = BranchFactory(status=Branch.Status.INACTIVE)
        client.force_login(actor)
        response = client.post(reverse("locations:branch_activate", kwargs={"uuid": target.uuid}))
        assert response.status_code == 302
        target.refresh_from_db()
        assert target.status == Branch.Status.ACTIVE


class TestClientSitesIntegration:
    def test_client_detail_sites_tab_shows_real_sites(self, client):
        role = _role_with(
            (Permission.Module.CLIENT, Permission.Action.VIEW),
            (Permission.Module.SITE, Permission.Action.VIEW),
        )
        viewer = UserFactory(role=role)
        target_client = ClientFactory()
        SiteFactory(client=target_client, site_name="LinkedSite")
        client.force_login(viewer)
        response = client.get(
            reverse("clients:client_detail", kwargs={"uuid": target_client.uuid}), {"tab": "sites"}
        )
        assert response.status_code == 200
        assert b"LinkedSite" in response.content
