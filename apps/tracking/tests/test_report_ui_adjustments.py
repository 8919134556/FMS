"""UI adjustments: the Live Tracking table's per-vehicle Live Map popup, the
Trip Report's rows no longer navigating to the vehicle page, and the Odometer
Report without summary cards (its filters, Device/GPS columns and exports
unchanged). Interaction itself is exercised in a browser; these guard the
server-rendered contract and the page scripts the behaviour depends on."""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

from apps.accounts.models import Permission
from apps.accounts.tests.factories import UserFactory
from apps.core.tests.factories import role_with

pytestmark = pytest.mark.django_db

STATIC_JS = Path(settings.BASE_DIR) / "static" / "js"
STATIC_CSS = Path(settings.BASE_DIR) / "static" / "css"


@pytest.fixture
def viewer(client):
    client.force_login(UserFactory(role=role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW))))
    return client


class TestLiveTrackingLiveMap:
    def test_page_has_the_action_column_and_the_one_vehicle_popup(self, viewer):
        html = viewer.get(reverse("tracking:live_map")).content.decode()
        assert "<th>Action</th>" in html
        assert 'id="liveVehicleModal"' in html and 'id="liveVehicleMap"' in html
        assert 'id="liveMap"' in html  # the main all-vehicles map is still there
        assert "data-route-url" not in html  # the popup shows the live position only, no route
        assert html.index('id="liveMap"') < html.index('id="vehicleStatusTable"')  # map above the table

    def test_live_map_reuses_the_page_marker_and_feed(self):
        js = (STATIC_JS / "live_tracking.js").read_text(encoding="utf-8")
        assert 'data-live-map="${vehicle.vehicle_uuid}"' in js
        assert "event.stopPropagation();" in js  # the button doesn't also select the row
        assert "icon: iconFor(vehicle)" in js  # same marker as the main map
        assert "if (state.liveMap.uuid) updateLiveMap();" in js  # rides on the page's refresh
        assert "FmsMap.addTileLayer(map)" in js

    def test_main_map_no_longer_fills_the_whole_viewport(self):
        css = (STATIC_CSS / "live_tracking.css").read_text(encoding="utf-8")
        shell = re.search(r"\.live-map-shell \{[^}]*\}", css).group(0)
        assert "calc(100vh - 340px)" not in shell and "min-height: 460px" not in shell
        assert "clamp(" in shell


class TestTripReportRowsDoNotNavigate:
    def test_script_has_no_row_navigation(self):
        js = (STATIC_JS / "trip_report.js").read_text(encoding="utf-8")
        assert "window.location" not in js
        assert "vehicleUrl" not in js and "vehicle-status-row" not in js
        assert "openRouteModal(trip)" in js  # the Map action is kept

    def test_page_no_longer_carries_the_vehicle_url_template(self, viewer):
        html = viewer.get(reverse("tracking:trip_report")).content.decode()
        assert "data-vehicle-url-template" not in html
        assert 'data-report-download="pdf"' in html and 'id="tripVehicleFilter"' in html


class TestOdometerReportWithoutCards:
    def test_no_summary_cards_but_filters_and_columns_remain(self, viewer):
        html = viewer.get(reverse("tracking:odometer_report")).content.decode()
        assert "kpi-card" not in html and "dashboard-kpi-grid" not in html and "odoKpi" not in html
        for pill in ("today", "yesterday", "last3", "last5", "last7", "custom"):
            assert f'data-range="{pill}"' in html
        for group in ("Device Odometer", "GPS Odometer"):
            for part in ("Start", "End", "Distance"):
                assert f'<span class="odo-th-group">{group}</span> {part}</th>' in html
        assert 'id="odoVehicleFilter"' in html and 'data-report-download="xlsx"' in html

    def test_script_does_not_look_for_the_removed_cards(self):
        js = (STATIC_JS / "odometer_report.js").read_text(encoding="utf-8")
        assert "odoKpi" not in js and "renderSummary" not in js and "window.location" not in js
