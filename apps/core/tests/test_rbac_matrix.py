"""End-to-end RBAC checks against the *real* seeded roles (seed_roles).

The invariant under test: for every role, the sidebar shows a link **if and
only if** that URL actually opens for the user — hiding is presentation, the
403 is the security, and the two must never disagree.
"""

import pytest
from django.core.management import call_command
from django.urls import reverse

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.clients.tests.factories import ClientFactory
from apps.core.permissions import has_admin_access, user_has_permission
from apps.core.tests.factories import role_with
from apps.accounts.models import Permission
from apps.trips.tests.factories import TripFactory
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db

# url name -> RBAC module whose "view" opens it. Deliberately excludes
# "trips:trip_list": the dispatch/logistics Trip list has no sidebar entry
# (removed in favour of Reports > Trip Report) but the page itself, and its
# permission check, are untouched — it's still reachable from the dashboard,
# Dispatch Board, and every Client/Driver/Vehicle "Trips" tab.
PROTECTED_LIST_URLS = {
    "dispatch:board": "trip",
    "routes:route_list": "route",
    "vehicles:vehicle_list": "vehicle",
    "vehicles:vehicle_type_list": "vehicle_type",
    "drivers:driver_list": "driver",
    "vehicles:assignment_list": "assignment",
    "clients:client_list": "client",
    "locations:site_list": "site",
    "contracts:contract_list": "contract",
    "vendors:vendor_list": "vendor",
    "locations:branch_list": "branch",
    "tracking:live_map": "tracking_device",
    "tracking:device_list": "tracking_device",
    "tracking:trip_report": "tracking_device",
    "tracking:odometer_report": "tracking_device",
    "tracking:location_report": "tracking_device",
    "geofences:geofence_list": "geofence",
    "maintenance:maintenance_list": "maintenance",
    "documents:document_list": "document",
    "alerts:alert_report": "alert",
    "accounts_portal:user_list": "user",
    "accounts_portal:role_list": "role",
    "audit:audit_log_list": "audit_log",
    "core:settings": "settings",
}

SYSTEM_ROLE_CODES = [
    "super-admin", "admin", "fleet-manager", "operations-manager", "dispatcher", "client-admin", "read-only",
]


@pytest.fixture
def seeded_roles(db):
    call_command("seed_roles", verbosity=0)


def _login_as(client, role_code):
    role = Role.objects.get(code=role_code)
    user = UserFactory(role=role)
    client.force_login(user)
    return user


def _nav_urls(response):
    return {item["url"] for section in response.context["nav_sections"] for item in section["items"]}


@pytest.mark.parametrize("role_code", SYSTEM_ROLE_CODES)
def test_sidebar_shows_a_module_if_and_only_if_its_url_opens(client, seeded_roles, role_code):
    user = _login_as(client, role_code)
    dashboard = client.get(reverse("core:dashboard"))
    assert dashboard.status_code == 200
    nav = _nav_urls(dashboard)

    for url_name, module in PROTECTED_LIST_URLS.items():
        url = reverse(url_name)
        allowed = user_has_permission(user, module, "view")
        response = client.get(url)
        if allowed:
            assert response.status_code == 200, f"{role_code} should open {url_name}"
            assert url in nav, f"{role_code}: {url_name} missing from sidebar"
        else:
            assert response.status_code == 403, f"{role_code} must get 403 on {url_name}"
            assert url not in nav, f"{role_code}: {url_name} must not be in the sidebar"


@pytest.mark.parametrize("role_code", SYSTEM_ROLE_CODES)
def test_unauthorized_sections_and_labels_are_absent_from_rendered_html(client, seeded_roles, role_code):
    user = _login_as(client, role_code)
    html = client.get(reverse("core:dashboard")).content.decode()
    # Locked/disabled placeholders are gone entirely, for everyone.
    assert "is-locked" not in html
    assert "You don&#x27;t have access to this module" not in html
    if not user_has_permission(user, "user", "view"):
        assert 'href="/admin/users/"' not in html
        assert "Roles &amp; Permissions" not in html
    if not user_has_permission(user, "settings", "view"):
        assert 'href="/dashboard/settings/"' not in html


def test_admin_and_super_admin_see_every_module(client, seeded_roles):
    for code in ("admin", "super-admin"):
        client.logout()
        _login_as(client, code)
        response = client.get(reverse("core:dashboard"))
        nav = _nav_urls(response)
        for url_name in PROTECTED_LIST_URLS:
            assert reverse(url_name) in nav, f"{code} is missing {url_name}"
        assert reverse("core:master_data") in nav


def test_role_without_permissions_sees_only_the_dashboard(client):
    client.force_login(UserFactory(role=None))
    response = client.get(reverse("core:dashboard"))
    nav = _nav_urls(response)
    assert nav == {reverse("core:dashboard")}
    sections = response.context["nav_sections"]
    assert [s["heading"] for s in sections] == ["Overview"]


def test_master_data_link_needs_one_of_client_site_branch(client):
    client.force_login(UserFactory(role=role_with((Permission.Module.SITE, Permission.Action.VIEW))))
    response = client.get(reverse("core:dashboard"))
    assert reverse("core:master_data") in _nav_urls(response)
    assert client.get(reverse("core:master_data")).status_code == 200


# --------------------------------------------------------------------------
# Role-based dashboards
# --------------------------------------------------------------------------


class TestDashboardVariants:
    def test_admin_roles_get_the_admin_dashboard(self, client, seeded_roles):
        for code in ("admin", "super-admin"):
            client.logout()
            _login_as(client, code)
            response = client.get(reverse("core:dashboard"))
            assert "dashboard/admin.html" in [t.name for t in response.templates]
            assert b"Admin Dashboard" in response.content

    @pytest.mark.parametrize("role_code", ["fleet-manager", "operations-manager", "dispatcher", "client-admin"])
    def test_operational_roles_get_the_workspace_not_the_admin_dashboard(self, client, seeded_roles, role_code):
        _login_as(client, role_code)
        response = client.get(reverse("core:dashboard"))
        names = [t.name for t in response.templates]
        assert "dashboard/workspace.html" in names
        assert "dashboard/admin.html" not in names
        assert b"Admin Dashboard" not in response.content

    def test_workspace_never_leaks_user_role_or_audit_data(self, client, seeded_roles):
        TripFactory()
        _login_as(client, "fleet-manager")
        response = client.get(reverse("core:dashboard"))
        ctx = response.context
        assert ctx["total_users"] is None and ctx["active_users"] is None and ctx["total_roles"] is None
        assert ctx["logins_today_count"] is None and ctx["audit_today_count"] is None
        assert ctx["activity_scope"] == "mine"
        assert b"View full audit log" not in response.content
        assert b"Active Users" not in response.content

    def test_workspace_only_lists_the_users_own_activity(self, client, seeded_roles):
        from apps.audit.models import AuditLog

        other = UserFactory(role=None)
        AuditLog.objects.create(user=other, action=AuditLog.Action.CREATE, module="secret", entity="OtherUsersThing", entity_id="1")
        _login_as(client, "dispatcher")
        response = client.get(reverse("core:dashboard"))
        assert b"OtherUsersThing" not in response.content

    def test_workspace_content_follows_permissions(self, client, seeded_roles):
        VehicleFactory()
        _login_as(client, "fleet-manager")
        fleet = client.get(reverse("core:dashboard"))
        assert b"Total Vehicles" in fleet.content
        assert b"Total Trips" not in fleet.content  # fleet manager has no trip.view

        client.logout()
        _login_as(client, "dispatcher")
        dispatch = client.get(reverse("core:dashboard"))
        assert b"Total Trips" in dispatch.content
        assert b"Maintenance Scheduled" not in dispatch.content  # dispatcher has no maintenance.view

    def test_user_with_no_modules_gets_a_friendly_empty_workspace(self, client):
        client.force_login(UserFactory(role=None))
        response = client.get(reverse("core:dashboard"))
        assert response.status_code == 200
        assert b"No modules assigned yet" in response.content

    def test_custom_role_with_audit_view_gets_admin_dashboard_but_no_user_numbers(self, client):
        role = role_with((Permission.Module.AUDIT_LOG, Permission.Action.VIEW))
        user = UserFactory(role=role)
        assert has_admin_access(user)
        client.force_login(user)
        response = client.get(reverse("core:dashboard"))
        assert "dashboard/admin.html" in [t.name for t in response.templates]
        assert response.context["total_users"] is None
        assert response.context["activity_scope"] == "all"

    def test_role_names_cannot_break_out_of_the_inline_chart_script(self, client):
        from apps.accounts.tests.factories import RoleFactory

        RoleFactory(name="</script><b>x", code="xss-role")
        role = role_with(
            (Permission.Module.ROLE, Permission.Action.VIEW), (Permission.Module.USER, Permission.Action.VIEW)
        )
        client.force_login(UserFactory(role=role))
        response = client.get(reverse("core:dashboard"))
        assert b"</script><b>x" not in response.content


# --------------------------------------------------------------------------
# Server-side checks that don't depend on the UI at all
# --------------------------------------------------------------------------


class TestDirectAccess:
    def test_typing_a_restricted_url_is_403_even_though_it_is_not_in_the_sidebar(self, client, seeded_roles):
        _login_as(client, "dispatcher")
        for url_name in ("accounts_portal:user_list", "audit:audit_log_list", "core:settings", "clients:client_list"):
            assert client.get(reverse(url_name)).status_code == 403, url_name

    def test_post_actions_are_gated_too(self, client, seeded_roles):
        _login_as(client, "read-only")
        target = ClientFactory()
        assert client.post(reverse("clients:client_deactivate", kwargs={"uuid": target.uuid})).status_code == 403
        assert client.post(reverse("clients:client_create"), {}).status_code == 403

    def test_api_docs_require_login(self, client):
        assert client.get("/api/schema/").status_code in (401, 403, 302)
        assert client.get("/api/docs/").status_code in (401, 403, 302)

    def test_api_docs_open_for_logged_in_users(self, client, seeded_roles):
        _login_as(client, "admin")
        assert client.get("/api/schema/").status_code == 200

    def test_api_user_activate_is_permission_gated_not_superuser_only(self, client, seeded_roles):
        from rest_framework.test import APIClient

        admin_user = UserFactory(role=Role.objects.get(code="admin"))
        target = UserFactory(role=None, status="INACTIVE")
        api = APIClient()
        api.force_authenticate(admin_user)
        assert api.post(f"/api/v1/users/{target.uuid}/activate/").status_code == 200

        viewer = UserFactory(role=Role.objects.get(code="read-only"))
        api.force_authenticate(viewer)
        assert api.post(f"/api/v1/users/{target.uuid}/deactivate/").status_code == 403


class TestDetailTabsFollowPermissions:
    def test_vehicle_page_hides_tabs_for_modules_the_viewer_cannot_open(self, client):
        role = role_with((Permission.Module.VEHICLE, Permission.Action.VIEW))
        vehicle = VehicleFactory()
        client.force_login(UserFactory(role=role))
        url = reverse("vehicles:vehicle_detail", kwargs={"uuid": vehicle.uuid})
        response = client.get(url)
        tab_ids = {t["id"] for t in response.context["tab_list"]}
        assert tab_ids.isdisjoint({"trips", "maintenance", "documents", "gps", "history", "driver"})
        assert "overview" in tab_ids

    def test_requesting_a_hidden_tab_falls_back_to_overview(self, client):
        role = role_with((Permission.Module.VEHICLE, Permission.Action.VIEW))
        vehicle = VehicleFactory()
        client.force_login(UserFactory(role=role))
        response = client.get(reverse("vehicles:vehicle_detail", kwargs={"uuid": vehicle.uuid}), {"tab": "trips"})
        assert response.status_code == 200
        assert response.context["active_tab"] == "overview"

    def test_tabs_appear_when_the_module_permission_is_granted(self, client):
        role = role_with(
            (Permission.Module.VEHICLE, Permission.Action.VIEW),
            (Permission.Module.TRIP, Permission.Action.VIEW),
            (Permission.Module.AUDIT_LOG, Permission.Action.VIEW),
        )
        vehicle = VehicleFactory()
        client.force_login(UserFactory(role=role))
        response = client.get(reverse("vehicles:vehicle_detail", kwargs={"uuid": vehicle.uuid}), {"tab": "trips"})
        assert response.context["active_tab"] == "trips"
        assert {"trips", "history"} <= {t["id"] for t in response.context["tab_list"]}

    def test_client_overview_hides_cross_module_counts(self, client):
        role = role_with((Permission.Module.CLIENT, Permission.Action.VIEW))
        target = ClientFactory()
        client.force_login(UserFactory(role=role))
        response = client.get(reverse("clients:client_detail", kwargs={"uuid": target.uuid}))
        assert b"Active Trips" not in response.content
        assert b"Active Contracts" not in response.content
