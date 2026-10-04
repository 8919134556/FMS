"""Live Tracking auto-refresh: the interval selector, the reusable JS module,
the feed-health block on the fleet API, and the dedicated polling throttle."""

import datetime
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.throttling import UserRateThrottle

from apps.accounts.models import Permission
from apps.accounts.tests.factories import UserFactory
from apps.core.tests.factories import role_with
from apps.tracking import services
from apps.tracking.api_views import FleetCurrentTelemetryView
from apps.tracking.authentication import FleetReadRateThrottle
from apps.tracking.comms_sync import STATE_KEY
from apps.tracking.models import CommsSyncState

pytestmark = pytest.mark.django_db

STATIC_JS = Path(settings.BASE_DIR) / "static" / "js"


def _viewer():
    return UserFactory(role=role_with((Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW)))


class TestIntervalSelector:
    def test_offers_exactly_the_five_intervals(self, client):
        client.force_login(_viewer())
        html = client.get(reverse("tracking:live_map")).content.decode()
        select = re.search(r'<select id="liveRefreshInterval".*?</select>', html, re.S).group(0)
        options = re.findall(r'<option value="(\d+)"[^>]*>\s*([^<]+?)\s*</option>', select)
        assert options == [
            ("5000", "5 seconds"), ("10000", "10 seconds"), ("30000", "30 seconds"),
            ("60000", "60 seconds"), ("300000", "5 minutes"),
        ]
        assert 'value="30000" selected' in select  # sensible default
        assert "Manual" not in select

    def test_page_loads_the_shared_module_before_the_page_script(self, client):
        client.force_login(_viewer())
        html = client.get(reverse("tracking:live_map")).content.decode()
        assert html.index("js/auto_refresh.js") < html.index("js/live_tracking.js")

    def test_admins_get_the_restart_hint_others_do_not(self, client):
        client.force_login(_viewer())
        assert 'data-can-manage-feed="0"' in client.get(reverse("tracking:live_map")).content.decode()
        admin = UserFactory(is_superuser=True, is_staff=True, role=None)
        client.force_login(admin)
        assert 'data-can-manage-feed="1"' in client.get(reverse("tracking:live_map")).content.decode()


class TestAutoRefreshModule:
    """The behaviour itself (single timer, no overlap, pause when hidden, back-off)
    is exercised in a real browser; these guard the contract the page relies on."""

    def setup_method(self):
        self.source = (STATIC_JS / "auto_refresh.js").read_text(encoding="utf-8")

    def test_uses_a_chained_timeout_never_an_interval(self):
        code = re.sub(r"/\*.*?\*/", "", self.source, flags=re.S)
        assert "setInterval(" not in code.replace("setIntervalMs(", "").replace("options.setInterval", "")
        assert "setTimeout(" in code

    def test_guards_and_teardown_are_present(self):
        for needle in ("if (inFlight) return inFlight", "visibilitychange", "pagehide", "AbortController", "retryAfterMs"):
            assert needle in self.source

    def test_live_tracking_delegates_to_it_instead_of_its_own_timer(self):
        page = (STATIC_JS / "live_tracking.js").read_text(encoding="utf-8")
        assert "FmsAutoRefresh.create(" in page
        assert "setInterval(fetchFleet" not in page and "scheduleAutoRefresh" not in page


class TestFleetFeedHealth:
    def _get(self, client):
        client.force_login(_viewer())
        return client.get(reverse("fleet-telemetry-current")).json()

    def test_absent_when_no_comms_database_is_configured(self, client, settings):
        settings.COMMS_APP_DATABASE_URL = ""
        assert self._get(client)["sync"] is None

    def test_healthy_when_the_bridge_ran_recently(self, client, settings):
        settings.COMMS_APP_DATABASE_URL = "postgres://x/y"
        CommsSyncState.objects.create(key=STATE_KEY, last_success_at=timezone.now() - datetime.timedelta(seconds=20))
        sync = self._get(client)["sync"]
        assert sync["enabled"] and sync["healthy"] and 0 <= sync["age_seconds"] < 60

    def test_unhealthy_when_the_bridge_stopped(self, client, settings):
        settings.COMMS_APP_DATABASE_URL = "postgres://x/y"
        CommsSyncState.objects.create(
            key=STATE_KEY, last_success_at=timezone.now() - datetime.timedelta(hours=3),
            last_error="OperationalError: password=hunter2 refused",
        )
        sync = self._get(client)["sync"]
        assert sync["healthy"] is False and sync["age_seconds"] >= 3 * 3600 - 5
        assert sync["has_error"] is True
        assert "hunter2" not in str(sync)  # raw error text never reaches the browser

    def test_never_synced_is_unhealthy_with_no_age(self, client, settings):
        settings.COMMS_APP_DATABASE_URL = "postgres://x/y"
        sync = self._get(client)["sync"]
        assert sync == {
            "enabled": True, "last_success_at": None, "age_seconds": None, "healthy": False, "has_error": False,
        }

    def test_service_is_none_without_configuration(self, settings):
        settings.COMMS_APP_DATABASE_URL = ""
        assert services.comms_sync_status() is None


class TestPollingThrottle:
    def test_fleet_feed_has_its_own_bucket(self):
        assert FleetCurrentTelemetryView.throttle_classes == [FleetReadRateThrottle]
        assert issubclass(FleetReadRateThrottle, UserRateThrottle) and FleetReadRateThrottle.scope == "fleet_read"

    def test_budget_covers_several_tabs_polling_every_five_seconds(self):
        rate = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["fleet_read"]
        per_hour = int(rate.split("/")[0])
        one_tab_at_5s = 3600 // 5
        assert per_hour >= 4 * one_tab_at_5s
        # ...and polling must not consume the general per-user budget.
        assert settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["user"] == "1000/hour"
