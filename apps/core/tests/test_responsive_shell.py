"""Guards for the shared responsive layer (templates + stylesheet contract)."""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

ROOT = Path(settings.BASE_DIR)
CSS = ROOT / "static" / "css"


@pytest.fixture
def admin_client(client):
    client.force_login(UserFactory(is_superuser=True, is_staff=True, role=None))
    return client


class TestShell:
    def test_viewport_meta_supports_notched_phones(self, admin_client):
        html = admin_client.get(reverse("core:dashboard")).content.decode()
        assert "width=device-width, initial-scale=1, viewport-fit=cover" in html

    def test_responsive_stylesheet_is_loaded_last(self, admin_client):
        html = admin_client.get(reverse("tracking:live_map")).content.decode()
        sheets = re.findall(r'<link rel="stylesheet" href="([^"]*\.css)"', html)
        assert sheets[-1].endswith("css/responsive.css")
        assert any(sheet.endswith("live_tracking.css") for sheet in sheets[:-1])

    def test_topbar_has_phone_title_and_labelled_icon_buttons(self, admin_client):
        html = admin_client.get(reverse("core:dashboard")).content.decode()
        assert 'class="topbar-title d-md-none"' in html
        assert 'aria-label="Search"' in html and 'aria-label="Quick add"' in html
        assert 'class="quick-add-label"' in html

    def test_wide_only_topbar_items_are_hidden_below_desktop(self, admin_client):
        html = admin_client.get(reverse("core:dashboard")).content.decode()
        assert "user-meta d-none d-lg-block" in html
        assert re.search(r'class="topbar-icon-btn d-none d-lg-inline-flex"[^>]*id="fmsFullscreenToggle"', html)


class TestComponents:
    def test_filter_bar_collapses_behind_a_toggle(self, admin_client):
        html = admin_client.get(reverse("vehicles:vehicle_list")).content.decode()
        assert "data-filter-bar-toggle" in html and "data-filter-bar-count" in html
        assert "filter-field-search" in html  # free-text fields get the full row on phones

    def test_table_card_header_exposes_hooks_for_stacking_actions(self, admin_client):
        html = admin_client.get(reverse("vehicles:vehicle_list")).content.decode()
        assert "table-card-title" in html and "table-card-actions" in html

    def test_no_hardcoded_white_text_on_list_pages(self):
        """`color:#fff` on a record name is invisible in the light theme."""
        offenders = [
            str(path.relative_to(ROOT))
            for path in (ROOT / "templates").rglob("*.html")
            if "registration" not in path.parts
            and path.name != "base_auth.html"
            and "color:#fff" in path.read_text(encoding="utf-8")
        ]
        assert offenders == []


class TestStylesheetContract:
    def setup_method(self):
        self.css = (CSS / "responsive.css").read_text(encoding="utf-8")

    def test_covers_phone_and_tablet_breakpoints(self):
        for query in ("(max-width: 575.98px)", "(max-width: 767.98px)", "(max-width: 991.98px)", "(max-width: 1199.98px)"):
            assert query in self.css

    def test_tables_stack_into_cards_and_keep_dropdowns_unclipped(self):
        assert ".table.is-stacked" in self.css and "attr(data-label)" in self.css
        row_rule = re.search(r"\.table\.is-stacked > tbody > tr \{(.*?)\}", self.css, re.S).group(1)
        assert "overflow: hidden" not in row_rule.replace("/* no overflow:hidden — it would clip the row's dropdown menu */", "")

    def test_inputs_are_16px_on_touch_screens_to_avoid_ios_zoom(self):
        assert re.search(r"\.form-control[^{]*\{[^}]*font-size: 16px", self.css)

    def test_kpi_grid_is_a_shared_component_not_dashboard_only(self):
        assert ".dashboard-kpi-grid {" in (CSS / "components.css").read_text(encoding="utf-8")
        assert ".dashboard-kpi-grid {" not in (CSS / "dashboard.css").read_text(encoding="utf-8")


class TestTableLabelling:
    def test_app_js_labels_cells_from_headers_and_skips_spanning_rows(self):
        js = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
        assert "dataset.label" in js and "stack-skip" in js and 'classList.contains("no-stack")' in js
        assert "htmx:afterSwap" in js  # re-labelled after fragment swaps
