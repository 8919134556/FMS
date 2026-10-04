import pytest
from django.urls import reverse

from apps.accounts.models import Permission
from apps.accounts.tests.factories import UserFactory
from apps.clients.models import Client
from apps.clients.tests.factories import ClientFactory
from apps.core.tests.factories import role_with
from apps.locations.models import Branch
from apps.locations.tests.factories import BranchFactory, SiteFactory

pytestmark = pytest.mark.django_db


class TestMasterDataAccess:
    def test_anonymous_redirected_to_login(self, client):
        response = client.get(reverse("core:master_data"))
        assert response.status_code == 302

    def test_authenticated_user_without_permissions_gets_403(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("core:master_data"))
        assert response.status_code == 403


class TestMasterDataPermissionAwareCards:
    def test_client_only_permission_shows_only_client_card(self, client):
        role = role_with((Permission.Module.CLIENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        ClientFactory()
        client.force_login(viewer)
        response = client.get(reverse("core:master_data"))
        assert response.status_code == 200
        assert b"Manage Clients" in response.content
        assert b"Manage Sites" not in response.content
        assert b"Manage Branches" not in response.content

    def test_full_permissions_show_real_counts(self, client):
        role = role_with(
            (Permission.Module.CLIENT, Permission.Action.VIEW),
            (Permission.Module.SITE, Permission.Action.VIEW),
            (Permission.Module.BRANCH, Permission.Action.VIEW),
        )
        viewer = UserFactory(role=role)
        ClientFactory()
        ClientFactory(status=Client.Status.INACTIVE)
        SiteFactory()  # also creates its own client via SubFactory
        BranchFactory()
        BranchFactory(status=Branch.Status.INACTIVE)
        client.force_login(viewer)
        response = client.get(reverse("core:master_data"))
        assert response.status_code == 200
        content = response.content.decode()
        assert response.context["client_count"] == 3
        assert response.context["active_client_count"] == 2
        assert response.context["site_count"] == 1
        assert response.context["branch_count"] == 2
        assert response.context["active_branch_count"] == 1
        assert "Manage Clients" in content
        assert "Manage Sites" in content
        assert "Manage Branches" in content

    def test_recently_added_lists_real_records(self, client):
        role = role_with((Permission.Module.CLIENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        c = ClientFactory(client_name="Acme Freight")
        client.force_login(viewer)
        response = client.get(reverse("core:master_data"))
        assert c.client_name.encode() in response.content


class TestMasterDataQueryPerformance:
    def test_recently_added_query_count_does_not_grow_with_row_count(self, client, django_assert_max_num_queries):
        role = role_with(
            (Permission.Module.CLIENT, Permission.Action.VIEW),
            (Permission.Module.SITE, Permission.Action.VIEW),
            (Permission.Module.BRANCH, Permission.Action.VIEW),
        )
        viewer = UserFactory(role=role)
        for _ in range(10):
            ClientFactory()
            SiteFactory()
            BranchFactory()

        client.force_login(viewer)
        with django_assert_max_num_queries(20):
            response = client.get(reverse("core:master_data"))
        assert response.status_code == 200
