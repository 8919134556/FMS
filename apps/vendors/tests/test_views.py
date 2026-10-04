import pytest
from django.urls import reverse

from apps.accounts.models import Permission, RolePermission
from apps.accounts.tests.factories import RoleFactory, UserFactory
from apps.vendors.models import Vendor
from apps.vendors.tests.factories import VendorFactory

pytestmark = pytest.mark.django_db


def _role_with(*module_actions):
    role = RoleFactory()
    for module, action in module_actions:
        permission, _ = Permission.objects.get_or_create(module=module, action=action)
        RolePermission.objects.create(role=role, permission=permission)
    return role


class TestVendorListView:
    def test_anonymous_redirected_to_login(self, client):
        response = client.get(reverse("vendors:vendor_list"))
        assert response.status_code == 302

    def test_user_without_permission_gets_403(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("vendors:vendor_list"))
        assert response.status_code == 403

    def test_user_with_view_permission_sees_list(self, client):
        role = _role_with((Permission.Module.VENDOR, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        VendorFactory.create_batch(2)
        client.force_login(viewer)
        response = client.get(reverse("vendors:vendor_list"))
        assert response.status_code == 200
        assert b"Vendors" in response.content

    def test_search_filters_results(self, client):
        role = _role_with((Permission.Module.VENDOR, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        VendorFactory(vendor_name="Northgate Logistics")
        client.force_login(viewer)
        response = client.get(reverse("vendors:vendor_list"), {"q": "Northgate"})
        assert response.status_code == 200
        assert b"Northgate Logistics" in response.content

    def test_vendor_type_filter(self, client):
        role = _role_with((Permission.Module.VENDOR, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        VendorFactory(vendor_name="FuelCo", vendor_type=Vendor.VendorType.FUEL)
        VendorFactory(vendor_name="MaintCo", vendor_type=Vendor.VendorType.MAINTENANCE)
        client.force_login(viewer)
        response = client.get(reverse("vendors:vendor_list"), {"vendor_type": Vendor.VendorType.FUEL})
        assert b"FuelCo" in response.content
        assert b"MaintCo" not in response.content

    def test_export_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("vendors:vendor_export"))
        assert response.status_code == 403

    def test_export_returns_csv(self, client):
        role = _role_with((Permission.Module.VENDOR, Permission.Action.EXPORT))
        actor = UserFactory(role=role)
        VendorFactory(vendor_name="ExportedVendor")
        client.force_login(actor)
        response = client.get(reverse("vendors:vendor_export"))
        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv"
        assert b"ExportedVendor" in response.content


class TestVendorCreateView:
    def test_create_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("vendors:vendor_create"))
        assert response.status_code == 403

    def test_create_vendor_success(self, client):
        role = _role_with(
            (Permission.Module.VENDOR, Permission.Action.VIEW),
            (Permission.Module.VENDOR, Permission.Action.CREATE),
        )
        actor = UserFactory(role=role)
        client.force_login(actor)
        response = client.post(
            reverse("vendors:vendor_create"),
            {
                "vendor_code": "VNDNEW01", "vendor_name": "New Vendor",
                "vendor_type": Vendor.VendorType.VEHICLE_OWNER, "status": Vendor.Status.ACTIVE,
            },
        )
        assert response.status_code == 302
        assert Vendor.objects.filter(vendor_code="VNDNEW01").exists()

    def test_create_without_name_rejected(self, client):
        role = _role_with(
            (Permission.Module.VENDOR, Permission.Action.VIEW),
            (Permission.Module.VENDOR, Permission.Action.CREATE),
        )
        actor = UserFactory(role=role)
        client.force_login(actor)
        response = client.post(
            reverse("vendors:vendor_create"),
            {"vendor_code": "VNDNONAME", "vendor_type": Vendor.VendorType.OTHER, "status": Vendor.Status.ACTIVE},
        )
        assert response.status_code == 200
        assert not Vendor.objects.filter(vendor_code="VNDNONAME").exists()


class TestVendorDetailView:
    def test_detail_requires_permission(self, client):
        actor = UserFactory(role=None)
        target = VendorFactory()
        client.force_login(actor)
        response = client.get(reverse("vendors:vendor_detail", kwargs={"uuid": target.uuid}))
        assert response.status_code == 403

    def test_all_tabs_render(self, client):
        role = _role_with((Permission.Module.VENDOR, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        target = VendorFactory(vendor_name="TabTestVendor")
        client.force_login(viewer)
        for tab in ["overview", "vehicles", "contracts", "history"]:
            url = reverse("vendors:vendor_detail", kwargs={"uuid": target.uuid})
            response = client.get(url, {"tab": tab} if tab != "overview" else {})
            assert response.status_code == 200, f"tab={tab} failed"


class TestVendorLifecycleActions:
    def test_deactivate_vendor(self, client):
        role = _role_with(
            (Permission.Module.VENDOR, Permission.Action.VIEW),
            (Permission.Module.VENDOR, Permission.Action.ARCHIVE),
        )
        actor = UserFactory(role=role)
        target = VendorFactory(status=Vendor.Status.ACTIVE)
        client.force_login(actor)
        response = client.post(reverse("vendors:vendor_deactivate", kwargs={"uuid": target.uuid}))
        assert response.status_code == 302
        target.refresh_from_db()
        assert target.status == Vendor.Status.INACTIVE

    def test_activate_vendor(self, client):
        role = _role_with(
            (Permission.Module.VENDOR, Permission.Action.VIEW),
            (Permission.Module.VENDOR, Permission.Action.ARCHIVE),
        )
        actor = UserFactory(role=role)
        target = VendorFactory(status=Vendor.Status.INACTIVE)
        client.force_login(actor)
        response = client.post(reverse("vendors:vendor_activate", kwargs={"uuid": target.uuid}))
        assert response.status_code == 302
        target.refresh_from_db()
        assert target.status == Vendor.Status.ACTIVE
