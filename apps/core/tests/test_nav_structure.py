"""Main navigation structure: Overview → Tracking → Reports → Fleet →
Clients, with every report under Reports and no "Insight" section — the
same structure for every role, each role seeing only the entries its
permissions open (see test_rbac_matrix for the if-and-only-if invariant)."""

import pytest
from django.core.management import call_command
from django.urls import reverse

from apps.accounts.models import Permission, Role
from apps.accounts.tests.factories import UserFactory
from apps.core import context_processors
from apps.core.permissions import user_has_permission
from apps.core.tests.factories import role_with

pytestmark = pytest.mark.django_db

REPORT_ITEMS = [
    ("Trip Report", "tracking:trip_report", "tracking_device"),
    ("Alert Report", "alerts:alert_report", "alert"),
    ("Odometer Report", "tracking:odometer_report", "tracking_device"),
    ("Location Data Report", "tracking:location_report", "tracking_device"),
]
CANONICAL_HEADINGS = [s["heading"] for s in context_processors.NAV_SECTIONS]
ROLE_CODES = ["super-admin", "admin", "fleet-manager", "operations-manager", "dispatcher", "client-admin", "read-only"]


@pytest.fixture
def seeded_roles(db):
    call_command("seed_roles", verbosity=0)


def _sections(client):
    return client.get(reverse("core:dashboard")).context["nav_sections"]


def test_main_menu_order_puts_reports_between_tracking_and_fleet():
    assert CANONICAL_HEADINGS[:5] == ["Overview", "Tracking", "Reports", "Fleet", "Clients"]
    assert not [h for h in CANONICAL_HEADINGS if "insight" in h.lower()]


def test_reports_section_holds_every_report_and_tracking_no_longer_does():
    by_heading = {s["heading"]: s for s in context_processors.NAV_SECTIONS}
    reports = [(i["label"], i["url_name"], i["module"]) for i in by_heading["Reports"]["items"]]
    assert reports == REPORT_ITEMS
    assert "tracking:trip_report" not in {i["url_name"] for i in by_heading["Tracking"]["items"]}
    # Each report appears exactly once in the whole menu.
    all_url_names = [i["url_name"] for s in context_processors.NAV_SECTIONS for i in s["items"]]
    for _label, url_name, _module in REPORT_ITEMS:
        assert all_url_names.count(url_name) == 1


@pytest.mark.parametrize("role_code", ROLE_CODES)
def test_every_role_gets_the_same_structure_filtered_by_its_permissions(client, seeded_roles, role_code):
    user = UserFactory(role=Role.objects.get(code=role_code))
    client.force_login(user)
    sections = _sections(client)
    headings = [s["heading"] for s in sections]

    # Same order for everyone (a role only ever loses sections it has no access to).
    assert headings == [h for h in CANONICAL_HEADINGS if h in headings]
    assert "Insight" not in headings

    expected = [label for label, _url, module in REPORT_ITEMS if user_has_permission(user, module, "view")]
    reports = next((s for s in sections if s["heading"] == "Reports"), None)
    if expected:
        assert [i["label"] for i in reports["items"]] == expected
        for item in reports["items"]:
            assert client.get(item["url"]).status_code == 200, f"{role_code} cannot open {item['label']}"
    else:
        assert reports is None


def test_admin_sees_every_report(client, seeded_roles):
    client.force_login(UserFactory(role=Role.objects.get(code="admin")))
    reports = next(s for s in _sections(client) if s["heading"] == "Reports")
    assert [i["label"] for i in reports["items"]] == [label for label, *_ in REPORT_ITEMS]


def test_a_single_report_permission_shows_just_that_report(client):
    client.force_login(UserFactory(role=role_with((Permission.Module.ALERT, Permission.Action.VIEW))))
    reports = next(s for s in _sections(client) if s["heading"] == "Reports")
    assert [i["label"] for i in reports["items"]] == ["Alert Report"]


@pytest.mark.parametrize(
    "url_name, label", [(u, lbl) for lbl, u, _m in REPORT_ITEMS]
)
def test_report_pages_are_highlighted_under_reports(client, seeded_roles, url_name, label):
    client.force_login(UserFactory(role=Role.objects.get(code="admin")))
    response = client.get(reverse(url_name))
    assert response.context["active_nav_section"] == "Reports"
    assert response.context["active_nav_label"] == label
    html = response.content.decode()
    assert "Insight" not in html


def test_a_new_report_is_one_list_entry_away(client, monkeypatch, seeded_roles):
    """Scalability: adding a report type is a data change only."""
    sections = [dict(s) for s in context_processors.NAV_SECTIONS]
    reports = next(s for s in sections if s["key"] == "reports")
    reports["items"] = reports["items"] + [
        {"label": "Future Report", "icon": "graph-up", "url_name": "tracking:trip_report", "module": "tracking_device", "phase": 3},
    ]
    monkeypatch.setattr(context_processors, "NAV_SECTIONS", sections)
    client.force_login(UserFactory(role=Role.objects.get(code="admin")))
    html = client.get(reverse("core:dashboard")).content.decode()
    assert "Future Report" in html
