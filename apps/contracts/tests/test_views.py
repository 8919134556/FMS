import pytest
from django.urls import reverse

from apps.accounts.models import Permission, RolePermission
from apps.accounts.tests.factories import RoleFactory, UserFactory
from apps.clients.tests.factories import ClientFactory
from apps.contracts.models import Contract
from apps.contracts.tests.factories import ContractFactory
from apps.vendors.tests.factories import VendorFactory

pytestmark = pytest.mark.django_db


def _role_with(*module_actions):
    role = RoleFactory()
    for module, action in module_actions:
        permission, _ = Permission.objects.get_or_create(module=module, action=action)
        RolePermission.objects.create(role=role, permission=permission)
    return role


class TestContractListView:
    def test_anonymous_redirected_to_login(self, client):
        response = client.get(reverse("contracts:contract_list"))
        assert response.status_code == 302

    def test_user_without_permission_gets_403(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("contracts:contract_list"))
        assert response.status_code == 403

    def test_user_with_view_permission_sees_list(self, client):
        role = _role_with((Permission.Module.CONTRACT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        ContractFactory.create_batch(2)
        client.force_login(viewer)
        response = client.get(reverse("contracts:contract_list"))
        assert response.status_code == 200
        assert b"Contracts" in response.content

    def test_search_filters_results(self, client):
        role = _role_with((Permission.Module.CONTRACT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        ContractFactory(contract_number="CTR-777777")
        client.force_login(viewer)
        response = client.get(reverse("contracts:contract_list"), {"q": "777777"})
        assert response.status_code == 200
        assert b"CTR-777777" in response.content

    def test_client_filter(self, client):
        role = _role_with((Permission.Module.CONTRACT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        target_client = ClientFactory()
        ContractFactory(contract_number="CTR-888801", client=target_client)
        ContractFactory(contract_number="CTR-888802")
        client.force_login(viewer)
        response = client.get(reverse("contracts:contract_list"), {"client": target_client.id})
        assert b"CTR-888801" in response.content
        assert b"CTR-888802" not in response.content

    def test_export_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("contracts:contract_export"))
        assert response.status_code == 403

    def test_export_returns_csv(self, client):
        role = _role_with((Permission.Module.CONTRACT, Permission.Action.EXPORT))
        actor = UserFactory(role=role)
        ContractFactory(contract_number="CTR-999901")
        client.force_login(actor)
        response = client.get(reverse("contracts:contract_export"))
        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv"
        assert b"CTR-999901" in response.content


class TestContractCreateView:
    def test_create_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("contracts:contract_create"))
        assert response.status_code == 403

    def test_create_contract_success(self, client):
        role = _role_with(
            (Permission.Module.CONTRACT, Permission.Action.VIEW),
            (Permission.Module.CONTRACT, Permission.Action.CREATE),
        )
        actor = UserFactory(role=role)
        client.force_login(actor)
        response = client.post(
            reverse("contracts:contract_create"),
            {
                "contract_number": "CTR-NEW001", "contract_type": Contract.ContractType.LEASE,
                "status": Contract.Status.DRAFT,
            },
        )
        assert response.status_code == 302
        assert Contract.objects.filter(contract_number="CTR-NEW001").exists()

    def test_create_rejects_end_before_start(self, client):
        role = _role_with(
            (Permission.Module.CONTRACT, Permission.Action.VIEW),
            (Permission.Module.CONTRACT, Permission.Action.CREATE),
        )
        actor = UserFactory(role=role)
        client.force_login(actor)
        response = client.post(
            reverse("contracts:contract_create"),
            {
                "contract_number": "CTR-BADDATES", "contract_type": Contract.ContractType.LEASE,
                "status": Contract.Status.DRAFT,
                "start_date": "2026-06-01", "end_date": "2026-01-01",
            },
        )
        assert response.status_code == 200
        assert not Contract.objects.filter(contract_number="CTR-BADDATES").exists()


class TestContractDetailView:
    def test_detail_requires_permission(self, client):
        actor = UserFactory(role=None)
        target = ContractFactory()
        client.force_login(actor)
        response = client.get(reverse("contracts:contract_detail", kwargs={"uuid": target.uuid}))
        assert response.status_code == 403

    def test_all_tabs_render(self, client):
        role = _role_with((Permission.Module.CONTRACT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        target = ContractFactory(contract_number="CTR-TABTEST")
        client.force_login(viewer)
        for tab in ["overview", "vehicles", "history"]:
            url = reverse("contracts:contract_detail", kwargs={"uuid": target.uuid})
            response = client.get(url, {"tab": tab} if tab != "overview" else {})
            assert response.status_code == 200, f"tab={tab} failed"

    def test_vendor_link_shown(self, client):
        role = _role_with((Permission.Module.CONTRACT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        vendor = VendorFactory(vendor_name="LinkedVendorCo")
        target = ContractFactory(vendor=vendor)
        client.force_login(viewer)
        response = client.get(reverse("contracts:contract_detail", kwargs={"uuid": target.uuid}))
        assert b"LinkedVendorCo" in response.content


class TestContractLifecycleActions:
    def test_terminate_contract(self, client):
        role = _role_with(
            (Permission.Module.CONTRACT, Permission.Action.VIEW),
            (Permission.Module.CONTRACT, Permission.Action.ARCHIVE),
        )
        actor = UserFactory(role=role)
        target = ContractFactory(status=Contract.Status.ACTIVE)
        client.force_login(actor)
        response = client.post(reverse("contracts:contract_terminate", kwargs={"uuid": target.uuid}))
        assert response.status_code == 302
        target.refresh_from_db()
        assert target.status == Contract.Status.TERMINATED

    def test_terminate_requires_archive_permission(self, client):
        role = _role_with((Permission.Module.CONTRACT, Permission.Action.VIEW))
        actor = UserFactory(role=role)
        target = ContractFactory(status=Contract.Status.ACTIVE)
        client.force_login(actor)
        response = client.post(reverse("contracts:contract_terminate", kwargs={"uuid": target.uuid}))
        assert response.status_code == 403

    def test_renew_contract(self, client):
        role = _role_with(
            (Permission.Module.CONTRACT, Permission.Action.VIEW),
            (Permission.Module.CONTRACT, Permission.Action.UPDATE),
        )
        actor = UserFactory(role=role)
        target = ContractFactory(status=Contract.Status.EXPIRING)
        client.force_login(actor)
        response = client.post(reverse("contracts:contract_renew", kwargs={"uuid": target.uuid}))
        assert response.status_code == 302
        target.refresh_from_db()
        assert target.status == Contract.Status.RENEWED
