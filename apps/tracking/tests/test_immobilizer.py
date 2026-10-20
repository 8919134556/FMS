"""Vehicle immobilizer ON/OFF (apps.tracking.immobilizer + the Live Map popup API).

The comms command API is faked at ``immobilizer._open`` — no test ever reaches a
real device. Two statuses stay apart throughout: the COMMAND's progress
(ImmobilizerCommand, from the comms API) and the ACTUAL state
(VehicleCurrentTelemetry.immobilizer, from the comms current_table "immobalizer")."""

import datetime
import io
import json
import urllib.error
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Permission
from apps.accounts.tests.factories import UserFactory
from apps.clients.tests.factories import ClientFactory
from apps.core.tests.factories import role_with
from apps.tracking import comms_sync, immobilizer
from apps.tracking.models import ImmobilizerCommand, VehicleCurrentTelemetry
from apps.tracking.tests.factories import TrackingDeviceFactory
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db

VIEW = (Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW)
COMMAND = (Permission.Module.TRACKING_DEVICE, Permission.Action.COMMAND)


class FakeCommandApi:
    """Stands in for the comms command API: records every request, answers from ``statuses``."""

    def __init__(self):
        self.requests = []
        self.next_id = 76
        self.statuses = {}  # command_id -> (status, status_name)
        self.unit_override = None
        self.fail_with = None

    def __call__(self, request, timeout):
        body = json.loads(request.data) if request.data else None
        self.requests.append({"method": request.get_method(), "url": request.full_url, "body": body,
                              "headers": dict(request.header_items())})
        if self.fail_with:
            raise self.fail_with
        if request.get_method() == "POST":
            command_id, self.next_id = self.next_id, self.next_id + 1
            self.statuses[command_id] = (0, "pending")
            self.units = getattr(self, "units", {})
            self.units[command_id] = body["unit_no"]
            return io.BytesIO(json.dumps({"status": "success", "command_id": command_id, "hex_frame": "00AB"}).encode())
        command_id = int(request.full_url.rsplit("/", 1)[1])
        status, name = self.statuses[command_id]
        unit = self.unit_override or self.units[command_id]
        return io.BytesIO(json.dumps({"command_id": command_id, "unit_no": unit, "status": status,
                                      "status_name": name, "hex_frame": "00AB"}).encode())


@pytest.fixture
def api(monkeypatch, settings):
    settings.COMMS_COMMAND_API_URL = "http://commands.test:9009"
    settings.COMMS_COMMAND_API_KEY = ""
    fake = FakeCommandApi()
    monkeypatch.setattr(immobilizer, "_open", fake)
    return fake


def _vehicle(imei="350424067315497", client=None, immobilizer_state=None):
    vehicle = VehicleFactory(**({"client": client} if client else {}))
    device = TrackingDeviceFactory(vehicle=vehicle, imei=imei)
    VehicleCurrentTelemetry.objects.create(
        vehicle=vehicle, device=device, timestamp=timezone.now(), latitude=12.98, longitude=77.53,
        immobilizer=immobilizer_state,
    )
    return vehicle


def _login(http, *perms, **user_kwargs):
    user = UserFactory(role=role_with(*perms), **user_kwargs)
    http.force_login(user)
    return user


def _send(client, vehicle, state):
    return client.post(reverse("vehicle-immobilizer", kwargs={"vehicle_uuid": vehicle.uuid}),
                       data={"state": state}, content_type="application/json")


def _status(client, body):
    return client.get(reverse("immobilizer-command-status", kwargs={"command_uuid": body["command"]["uuid"]})).json()


# 1, 2 — the request body: this vehicle's own unit number, the requested state ---------------------------

def test_turn_on_sends_the_vehicles_own_unit_no(client, api):
    _vehicle(imei="111111111111111")  # another vehicle, must not be used
    vehicle = _vehicle(imei="350424067315497")
    _login(client, VIEW, COMMAND)
    res = _send(client, vehicle, "on")
    assert res.status_code == 202
    [sent] = api.requests
    assert sent["method"] == "POST" and sent["url"] == "http://commands.test:9009/commands/immobilizer"
    assert sent["body"] == {"unit_no": "350424067315497", "state": "on"}
    command = res.json()["command"]
    assert command["command_id"] == 76 and command["status"] == 0 and command["status_label"] == "Pending"
    assert command["in_progress"] is True
    stored = ImmobilizerCommand.objects.get()
    assert (stored.vehicle, stored.unit_no, stored.requested_state, stored.command_id) == (vehicle, "350424067315497", "on", 76)


def test_turn_off_body_and_api_key_header(client, api, settings):
    settings.COMMS_COMMAND_API_KEY = "secret-key"
    vehicle = _vehicle(imei="350424067315498", immobilizer_state=True)
    _login(client, VIEW, COMMAND)
    assert _send(client, vehicle, "OFF").status_code == 202
    sent = api.requests[0]
    assert sent["body"] == {"unit_no": "350424067315498", "state": "off"}
    assert sent["headers"].get("X-api-key") == "secret-key"  # server-side only, never in the page


def test_invalid_state_is_refused_without_calling_the_api(client, api):
    vehicle = _vehicle()
    _login(client, VIEW, COMMAND)
    res = _send(client, vehicle, "toggle")
    assert res.status_code == 400 and "on" in res.json()["detail"]
    assert api.requests == [] and not ImmobilizerCommand.objects.exists()


@pytest.mark.parametrize("imei", ["", "12"])
def test_vehicle_without_a_valid_unit_no_cannot_be_commanded(client, api, imei):
    vehicle = VehicleFactory()
    if imei:
        TrackingDeviceFactory(vehicle=vehicle, imei=imei)
    _login(client, VIEW, COMMAND)
    res = _send(client, vehicle, "on")
    assert res.status_code == 400 and "unit number" in res.json()["detail"]
    assert res.json()["has_unit_no"] is False
    assert api.requests == []


# 3, 4 — the returned command_id drives the status checks; all five statuses ------------------------------

@pytest.mark.parametrize("status, name, label, in_progress", [
    (0, "pending", "Pending", True),
    (1, "processing", "Processing", True),
    (2, "sent", "Sent to device", True),
    (3, "failed", "Failed", False),
    (4, "completed", "Completed", False),
])
def test_status_poll_uses_the_returned_command_id_and_maps_every_status(client, api, status, name, label, in_progress):
    api.next_id = 9123
    vehicle = _vehicle()
    _login(client, VIEW, COMMAND)
    body = _send(client, vehicle, "on").json()
    api.statuses[9123] = (status, name)
    polled = _status(client, body)
    assert api.requests[-1]["method"] == "GET" and api.requests[-1]["url"].endswith("/commands/9123")
    command = polled["command"]
    assert (command["status"], command["status_label"], command["in_progress"]) == (status, label, in_progress)
    assert command["status_name"] == name


def test_finished_commands_are_not_polled_again(client, api):
    vehicle = _vehicle()
    _login(client, VIEW, COMMAND)
    body = _send(client, vehicle, "on").json()
    api.statuses[76] = (4, "completed")
    _status(client, body)
    calls = len(api.requests)
    _status(client, body)
    assert len(api.requests) == calls


# 5, 6 — command status and actual state stay apart ---------------------------------------------------------

@pytest.mark.parametrize("final", [(3, "failed"), (4, "completed")])
def test_a_finished_command_never_changes_the_actual_state(client, api, final):
    vehicle = _vehicle(immobilizer_state=False)
    _login(client, VIEW, COMMAND)
    body = _send(client, vehicle, "on").json()
    api.statuses[76] = final
    polled = _status(client, body)
    assert polled["actual"]["immobilizer"] is False  # still OFF: only the device data can change it
    assert VehicleCurrentTelemetry.objects.get(vehicle=vehicle).immobilizer is False
    if final[0] == 3:
        assert polled["command"]["error"]


def test_actual_state_comes_from_the_comms_current_table():
    vehicle = VehicleFactory()
    TrackingDeviceFactory(vehicle=vehicle, imei="350424067315497")
    now = timezone.now().astimezone(comms_sync._local_timezone()).replace(tzinfo=None, microsecond=0)
    row = {"id": 1, "unitno": "350424067315497", "tracktime": now, "lat": 12.98, "lon": 77.53, "speed": 0.0,
           "direction": "0", "ignition": 0, "odometer": 1000, "gpsodometer": 0, "gpsstatus": 1, "location": "x"}
    comms_sync.apply_rows([{**row, "immobalizer": 1}], comms_sync.SyncReport())
    assert VehicleCurrentTelemetry.objects.get(vehicle=vehicle).immobilizer is True
    comms_sync.apply_rows([{**row, "immobalizer": 0}], comms_sync.SyncReport())  # same reading, value changed
    assert VehicleCurrentTelemetry.objects.get(vehicle=vehicle).immobilizer is False
    later = {**row, "id": 2, "tracktime": now + datetime.timedelta(seconds=30)}  # a history row: no column
    comms_sync.apply_rows([later], comms_sync.SyncReport())
    assert VehicleCurrentTelemetry.objects.get(vehicle=vehicle).immobilizer is False  # kept, never wiped
    assert "immobalizer" in comms_sync._CURRENT_COLUMNS


# 7, 8 — feed value + marker colour rule ---------------------------------------------------------------------

@pytest.mark.parametrize("value", [True, False, None])
def test_fleet_feed_carries_the_actual_state(client, value):
    _vehicle(immobilizer_state=value)
    _login(client, VIEW)
    [item] = client.get(reverse("fleet-telemetry-current")).json()["results"]
    assert item["immobilizer"] is value


def test_marker_is_black_when_immobilized_else_the_existing_colours():
    base = Path(settings.BASE_DIR) / "static"
    js = (base / "js" / "vehicle_icons.js").read_text(encoding="utf-8")
    css = (base / "css" / "live_tracking.css").read_text(encoding="utf-8")
    assert 'pin: immobilized ? "immobilized" : alert ? "alert" : key' in js  # priority
    assert "const immobilized = vehicle.immobilizer === true;" in js  # actual state only (null/false: normal)
    assert css.index(".vm.vm--alert {") < css.index(".vm.vm--immobilized { --vm-pin: #111827; }")  # later rule wins
    assert ".vm--alert::before" in css  # the alert halo stays on a black pin


# 9 — vehicles never share commands ----------------------------------------------------------------------------

def test_each_vehicle_has_its_own_commands(client, api):
    a = _vehicle(imei="350424067315497")
    b = _vehicle(imei="350424067315499")
    _login(client, VIEW, COMMAND)
    _send(client, a, "on")
    res_b = client.get(reverse("vehicle-immobilizer", kwargs={"vehicle_uuid": b.uuid})).json()
    assert res_b["command"] is None
    assert _send(client, b, "off").status_code == 202  # a's running command doesn't block b
    assert [r["body"]["unit_no"] for r in api.requests if r["method"] == "POST"] == ["350424067315497", "350424067315499"]


def test_a_status_for_another_command_or_unit_is_ignored(client, api):
    vehicle = _vehicle()
    _login(client, VIEW, COMMAND)
    body = _send(client, vehicle, "on").json()
    api.statuses[76] = (4, "completed")
    api.unit_override = "999999999999999"
    polled = _status(client, body)
    assert polled["command"]["status"] == 0 and polled["command"]["in_progress"] is True
    assert "different command" in polled["warning"]


# 10 — errors, timeouts, duplicates ------------------------------------------------------------------------------

def test_second_command_while_one_is_in_progress_is_refused(client, api):
    vehicle = _vehicle()
    _login(client, VIEW, COMMAND)
    assert _send(client, vehicle, "on").status_code == 202
    res = _send(client, vehicle, "on")
    assert res.status_code == 409 and "in progress" in res.json()["detail"]
    assert len([r for r in api.requests if r["method"] == "POST"]) == 1


def test_unreachable_api_records_a_rejected_command(client, api):
    api.fail_with = urllib.error.URLError("connection refused")
    vehicle = _vehicle(immobilizer_state=False)
    _login(client, VIEW, COMMAND)
    res = _send(client, vehicle, "on")
    assert res.status_code == 502 and "could not be reached" in res.json()["detail"]
    command = ImmobilizerCommand.objects.get()
    assert command.command_id is None and command.finished_at is not None and command.error
    api.fail_with = None
    assert _send(client, vehicle, "on").status_code == 202  # not blocked by the failed attempt


def test_api_refusing_the_command(client, api, monkeypatch):
    vehicle = _vehicle()
    _login(client, VIEW, COMMAND)
    monkeypatch.setattr(immobilizer, "_open", lambda request, timeout: io.BytesIO(b'{"status": "error"}'))
    res = _send(client, vehicle, "on")
    assert res.status_code == 502 and "did not accept" in res.json()["detail"]


def test_network_error_while_polling_is_not_a_failure(client, api):
    vehicle = _vehicle()
    _login(client, VIEW, COMMAND)
    body = _send(client, vehicle, "on").json()
    api.fail_with = urllib.error.URLError("timed out")
    polled = _status(client, body)
    assert polled["command"]["in_progress"] is True and polled["command"]["status"] == 0
    assert "could not be reached" in polled["warning"]


def test_command_times_out_and_frees_the_vehicle(client, api, settings):
    settings.IMMOBILIZER_COMMAND_TIMEOUT_SECONDS = 30
    vehicle = _vehicle()
    _login(client, VIEW, COMMAND)
    body = _send(client, vehicle, "on").json()
    ImmobilizerCommand.objects.update(created_at=timezone.now() - datetime.timedelta(seconds=60))
    api.statuses[76] = (2, "sent")
    polled = _status(client, body)["command"]
    assert polled["timed_out"] is True and polled["in_progress"] is False and polled["status"] == 2
    assert _send(client, vehicle, "off").status_code == 202


# Permissions and scope ----------------------------------------------------------------------------------------

def test_view_only_users_see_the_state_but_cannot_send(client, api):
    vehicle = _vehicle(immobilizer_state=True)
    _login(client, VIEW)
    info = client.get(reverse("vehicle-immobilizer", kwargs={"vehicle_uuid": vehicle.uuid})).json()
    assert info["actual"]["immobilizer"] is True and info["can_command"] is False
    assert _send(client, vehicle, "off").status_code == 403
    assert api.requests == []


def test_other_clients_vehicles_are_out_of_reach(client, api):
    mine, theirs = ClientFactory(), ClientFactory()
    other = _vehicle(client=theirs)
    _login(client, VIEW, COMMAND, client=mine)
    assert client.get(reverse("vehicle-immobilizer", kwargs={"vehicle_uuid": other.uuid})).status_code == 404
    assert _send(client, other, "on").status_code == 403  # client users are read-only: never send commands
    own = _vehicle(imei="350424067315490", client=mine, immobilizer_state=False)
    info = client.get(reverse("vehicle-immobilizer", kwargs={"vehicle_uuid": own.uuid})).json()
    assert info["actual"]["immobilizer"] is False and info["can_command"] is False
    assert _send(client, own, "on").status_code == 403
    assert api.requests == []


def test_live_page_wires_the_panel_to_fms_endpoints_only(client):
    _login(client, VIEW)
    html = client.get(reverse("tracking:live_map")).content.decode()
    assert "data-immobilizer-url-template=" in html and "data-immobilizer-command-url-template=" in html
    assert "9009" not in html  # the comms API address never reaches the browser
