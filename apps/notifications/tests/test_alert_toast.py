"""The compact top-right alert toast (static/js/alert_center.js): its one-line
detail comes from the alert's own data, and the payload the bell hands it
carries that line. Presentation only — nothing here changes an alert."""

from decimal import Decimal
from pathlib import Path

import pytest
from django.conf import settings
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.alerts.models import Alert
from apps.alerts.tests.factories import AlertFactory
from apps.notifications import services
from apps.notifications.models import Notification

pytestmark = pytest.mark.django_db


def _alert(category, **extra):
    return AlertFactory(category=category, occurred_at=timezone.now(), **extra)


@pytest.mark.parametrize("category, extra, expected", [
    (Alert.Category.PANIC, {"message": "Some long address, Bengaluru"}, "Panic button pressed"),
    (Alert.Category.OVER_SPEEDING, {"speed": Decimal("72.4")}, "Speed: 72 km/h"),
    (Alert.Category.MAIN_POWER_DISCONNECTED, {"message": "Main power disconnected (0.00 V)."}, "Main power disconnected (0.00 V)"),
    (Alert.Category.DEVICE_BATTERY_DISCONNECTED, {"message": "Device battery disconnected."}, "Device battery disconnected"),
    (Alert.Category.IDLE, {"message": ""}, "Idle"),  # no message -> the alert type
])
def test_detail_line_from_the_alert_data(category, extra, expected):
    assert services.alert_toast_detail(_alert(category, **extra)) == expected


@pytest.fixture
def fence():
    from apps.geofences.tests.factories import GeofenceFactory

    return GeofenceFactory(name="Bangalore City")


def test_geofence_entry_exit_name_the_geofence(fence):
    entry = _alert(Alert.Category.GEOFENCE_ENTRY, content_object=fence)
    exit_ = _alert(Alert.Category.GEOFENCE_EXIT, content_object=fence)
    assert services.alert_toast_detail(entry) == "Entered: Bangalore City"
    assert services.alert_toast_detail(exit_) == "Exited: Bangalore City"


def test_announcement_payload_carries_the_detail_and_level():
    user = UserFactory()
    alert = _alert(Alert.Category.OVER_SPEEDING, severity=Alert.Severity.HIGH, speed=Decimal("64"))
    Notification.objects.create(recipient=user, title="t", body="m", alert=alert)
    [item] = services.unread_alert_announcements(user)
    assert item["detail"] == "Speed: 64 km/h"
    assert item["severity"] == "HIGH" and item["level_label"] == "High"


def test_toast_is_compact_top_right_and_bounded():
    js = (Path(settings.BASE_DIR) / "static" / "js" / "alert_center.js").read_text(encoding="utf-8")
    css = (Path(settings.BASE_DIR) / "static" / "css" / "components.css").read_text(encoding="utf-8")
    assert "toastDurationMs: 7000, maxVisible: 3" in js  # configurable defaults
    assert "stack.prepend(card); // newest on top" in js
    stack = css[css.index(".fms-alert-stack {"):]
    stack = stack[:stack.index("}")]
    assert "right: 16px" in stack and "left: 50%" not in stack and "min(320px" in stack
    assert "fms-alert-popup" not in js  # the old large centred card is gone
