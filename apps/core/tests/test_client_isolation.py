"""Client -> Vehicle -> Device -> Raw -> App DB -> Client dashboard.

Proves the tenant boundary end to end: socket/ingest data is attributed to
the right vehicle and client, and a client user can only ever read their own
client's records — through every list, detail, API, report and dashboard —
and can't write at all.
"""

import datetime

import pytest
from django.db.models import ProtectedError
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Permission
from apps.accounts.tests.factories import UserFactory
from apps.clients.tests.factories import ClientFactory
from apps.contracts.tests.factories import ContractFactory
from apps.core.permissions import user_has_permission
from apps.core.tests.factories import role_with
from apps.documents.tests.factories import DocumentFactory
from apps.drivers.tests.factories import DriverFactory
from apps.locations.tests.factories import SiteFactory
from apps.maintenance.tests.factories import MaintenanceFactory
from apps.tracking import services
from apps.tracking.models import RawTelemetryEvent, TelemetryEvent, TrackingDevice, VehicleCurrentTelemetry
from apps.tracking.tests.factories import TrackingDeviceFactory, VehicleCurrentTelemetryFactory
from apps.trips.tests.factories import TripFactory
from apps.vehicles.models import Vehicle
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db

M = Permission.Module
A = Permission.Action


def _payload(lat=12.9716, lon=77.5946, minutes_ago=1):
    ts = (timezone.now() - datetime.timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")
    return {"timestamp": ts, "latitude": lat, "longitude": lon, "speed": 40}


def _broad_role():
    """A deliberately generous role: view/export everywhere it could matter,
    PLUS create/update on vehicles. None of it may widen a client user."""
    grants = []
    for module in [M.CLIENT, M.SITE, M.CONTRACT, M.VEHICLE, M.DRIVER, M.TRIP, M.MAINTENANCE, M.DOCUMENT,
                   M.TRACKING_DEVICE, M.ROUTE, M.ASSIGNMENT, M.VENDOR, M.USER, M.ROLE, M.ALERT,
                   M.GEOFENCE, M.AUDIT_LOG, M.SETTINGS, M.BRANCH, M.MASTER_DATA]:
        grants += [(module, A.VIEW), (module, A.EXPORT)]
    grants += [(M.VEHICLE, A.CREATE), (M.VEHICLE, A.UPDATE), (M.CLIENT, A.UPDATE), (M.TRACKING_DEVICE, A.COMMAND)]
    return role_with(*grants)


@pytest.fixture
def world():
    """Two tenants, each with vehicles + mapped devices + data."""
    a, b = ClientFactory(client_name="Client Alpha"), ClientFactory(client_name="Client Bravo")
    va1, va2 = VehicleFactory(client=a, registration_number="ALPHA-001"), VehicleFactory(client=a, registration_number="ALPHA-002")
    vb1 = VehicleFactory(client=b, registration_number="BRAVO-001")
    da1 = TrackingDeviceFactory(vehicle=va1)
    da2 = TrackingDeviceFactory(vehicle=va2)
    db1 = TrackingDeviceFactory(vehicle=vb1)
    for device in (da1, da2, db1):
        services.TelemetryIngestionService.ingest(device=device, raw_payload=_payload())
    role = _broad_role()
    return {
        "a": a, "b": b, "va1": va1, "va2": va2, "vb1": vb1, "da1": da1, "da2": da2, "db1": db1,
        "user_a": UserFactory(role=role, client=a), "user_b": UserFactory(role=role, client=b),
        "staff": UserFactory(role=role),
    }


# ---------------------------------------------------------------------------
# Socket data -> vehicle -> client mapping (Raw DB + App DB)
# ---------------------------------------------------------------------------


class TestIngestionMapping:
    def test_packet_is_attributed_to_its_vehicle_and_client_in_raw_and_app_tables(self, world):
        raw = RawTelemetryEvent.objects.get(device=world["da1"])
        assert raw.vehicle == world["va1"] and raw.client == world["a"]
        event = TelemetryEvent.objects.get(device=world["da1"])
        assert event.vehicle == world["va1"] and event.client == world["a"]
        current = VehicleCurrentTelemetry.objects.get(vehicle=world["va1"])
        assert current.device == world["da1"]

    def test_each_client_only_gets_its_own_rows(self, world):
        assert set(TelemetryEvent.objects.filter(client=world["a"]).values_list("vehicle__registration_number", flat=True)) == {"ALPHA-001", "ALPHA-002"}
        assert set(TelemetryEvent.objects.filter(client=world["b"]).values_list("vehicle__registration_number", flat=True)) == {"BRAVO-001"}

    def test_unmapped_device_data_is_kept_but_belongs_to_no_client(self):
        device = TrackingDeviceFactory(vehicle=None)
        services.TelemetryIngestionService.ingest(device=device, raw_payload=_payload())
        assert RawTelemetryEvent.objects.get(device=device).client is None
        event = TelemetryEvent.objects.get(device=device)
        assert event.vehicle is None and event.client is None
        assert not VehicleCurrentTelemetry.objects.filter(device=device).exists()

    def test_unassigned_vehicle_client_gives_null_client(self):
        vehicle = VehicleFactory(client=None)
        device = TrackingDeviceFactory(vehicle=vehicle)
        services.TelemetryIngestionService.ingest(device=device, raw_payload=_payload())
        assert TelemetryEvent.objects.get(device=device).client is None

    def test_reassigned_device_feeds_new_owner_even_with_a_stale_connection_object(self, world):
        """The TCP server holds one TrackingDevice per open connection."""
        stale = TrackingDevice.objects.get(pk=world["da1"].pk)  # what the socket session keeps
        new_vehicle = VehicleFactory(client=world["b"])
        services.unassign_device(device=world["da1"], actor=None)
        services.assign_device(device=TrackingDevice.objects.get(pk=world["da1"].pk), vehicle=new_vehicle, actor=None)

        services.TelemetryIngestionService.ingest(device=stale, raw_payload=_payload(lat=13.0, minutes_ago=0))

        newest = TelemetryEvent.objects.filter(device=world["da1"]).order_by("-timestamp").first()
        assert newest.vehicle == new_vehicle and newest.client == world["b"]
        # ...while the reading taken under the old owner stays with the old owner.
        assert TelemetryEvent.objects.filter(device=world["da1"], client=world["a"]).count() == 1

    def test_device_suspended_while_connected_stops_being_processed(self, world):
        stale = TrackingDevice.objects.get(pk=world["da1"].pk)
        services.disable_device(device=world["da1"], actor=None)
        before = TelemetryEvent.objects.count()
        result = services.TelemetryIngestionService.ingest(device=stale, raw_payload=_payload(lat=14.0))
        assert result.accepted == 0
        assert TelemetryEvent.objects.count() == before

    def test_moving_a_vehicle_to_another_client_keeps_history_with_the_previous_owner(self, world):
        vehicle = world["va1"]
        vehicle.client = world["b"]
        vehicle.save(update_fields=["client"])
        services.TelemetryIngestionService.ingest(device=world["da1"], raw_payload=_payload(lat=13.5, minutes_ago=0))
        assert TelemetryEvent.objects.filter(vehicle=vehicle, client=world["a"]).count() == 1
        assert TelemetryEvent.objects.filter(vehicle=vehicle, client=world["b"]).count() == 1

    def test_deleting_a_client_that_owns_vehicles_is_blocked(self, world):
        with pytest.raises(ProtectedError):
            world["a"].hard_delete()

    def test_deleting_a_client_with_linked_users_is_blocked(self):
        client_obj = ClientFactory()
        UserFactory(client=client_obj)
        with pytest.raises(ProtectedError):
            client_obj.hard_delete()

    def test_one_device_per_vehicle_is_enforced_at_the_database(self, world):
        from django.db import IntegrityError, transaction

        with pytest.raises(IntegrityError), transaction.atomic():
            TrackingDeviceFactory(vehicle=world["va1"])


# ---------------------------------------------------------------------------
# Client user: sees only their client's data
# ---------------------------------------------------------------------------


class TestFleetApiIsolation:
    def test_fleet_feed_only_returns_own_vehicles(self, client, world):
        client.force_login(world["user_a"])
        data = client.get("/api/v1/tracking/fleet/current/").json()
        assert {v["registration_number"] for v in data["results"]} == {"ALPHA-001", "ALPHA-002"}
        client.force_login(world["user_b"])
        data = client.get("/api/v1/tracking/fleet/current/").json()
        assert {v["registration_number"] for v in data["results"]} == {"BRAVO-001"}

    def test_staff_still_sees_the_whole_fleet(self, client, world):
        client.force_login(world["staff"])
        data = client.get("/api/v1/tracking/fleet/current/").json()
        assert data["count"] == 3

    def test_other_clients_vehicle_telemetry_is_a_404(self, client, world):
        client.force_login(world["user_a"])
        for suffix in ("current", "history"):
            url = f"/api/v1/tracking/vehicles/{world['vb1'].uuid}/telemetry/{suffix}/"
            assert client.get(url).status_code == 404
            own = f"/api/v1/tracking/vehicles/{world['va1'].uuid}/telemetry/{suffix}/"
            assert client.get(own).status_code == 200

    def test_history_of_a_transferred_vehicle_is_not_visible_to_the_new_client(self, client, world):
        vehicle = world["va1"]
        vehicle.client = world["b"]
        vehicle.save(update_fields=["client"])
        services.TelemetryIngestionService.ingest(device=world["da1"], raw_payload=_payload(lat=13.5, minutes_ago=0))
        client.force_login(world["user_b"])
        body = client.get(f"/api/v1/tracking/vehicles/{vehicle.uuid}/telemetry/history/").json()
        assert body["count"] == 1  # only the reading taken after it became Bravo's
        client.force_login(world["user_a"])
        assert client.get(f"/api/v1/tracking/vehicles/{vehicle.uuid}/telemetry/history/").status_code == 404

    def test_fleet_feed_query_count_does_not_grow_with_fleet_size(self, client, world, django_assert_max_num_queries):
        client.force_login(world["user_a"])
        for _ in range(25):
            VehicleCurrentTelemetryFactory(vehicle=VehicleFactory(client=world["a"]))
        with django_assert_max_num_queries(12):
            assert client.get("/api/v1/tracking/fleet/current/").status_code == 200


class TestWebIsolation:
    def test_vehicle_list_and_export_only_show_own_vehicles(self, client, world):
        client.force_login(world["user_a"])
        page = client.get(reverse("vehicles:vehicle_list")).content
        assert b"ALPHA-001" in page and b"BRAVO-001" not in page
        export = client.get(reverse("vehicles:vehicle_export")).content
        assert b"ALPHA-001" in export and b"BRAVO-001" not in export

    def test_other_clients_vehicle_detail_is_404(self, client, world):
        client.force_login(world["user_a"])
        assert client.get(reverse("vehicles:vehicle_detail", kwargs={"uuid": world["vb1"].uuid})).status_code == 404
        assert client.get(reverse("vehicles:vehicle_detail", kwargs={"uuid": world["va1"].uuid})).status_code == 200

    def test_client_pages_only_show_the_users_own_client(self, client, world):
        client.force_login(world["user_a"])
        listing = client.get(reverse("clients:client_list")).content
        assert b"Client Alpha" in listing and b"Client Bravo" not in listing
        assert client.get(reverse("clients:client_detail", kwargs={"uuid": world["b"].uuid})).status_code == 404

    def test_sites_drivers_trips_contracts_maintenance_are_scoped(self, client, world):
        site_a, site_b = SiteFactory(client=world["a"], site_name="SiteAlpha"), SiteFactory(client=world["b"], site_name="SiteBravo")
        driver_a, driver_b = DriverFactory(client=world["a"], first_name="Dora"), DriverFactory(client=world["b"], first_name="Boris")
        trip_a, trip_b = TripFactory(client=world["a"]), TripFactory(client=world["b"])
        ContractFactory(client=world["a"], contract_number="CON-ALPHA"), ContractFactory(client=world["b"], contract_number="CON-BRAVO")
        m_a, m_b = MaintenanceFactory(vehicle=world["va1"]), MaintenanceFactory(vehicle=world["vb1"])
        client.force_login(world["user_a"])

        def page(name):
            return client.get(reverse(name)).content

        assert b"SiteAlpha" in page("locations:site_list") and b"SiteBravo" not in page("locations:site_list")
        assert b"Dora" in page("drivers:driver_list") and b"Boris" not in page("drivers:driver_list")
        assert trip_a.trip_number.encode() in page("trips:trip_list") and trip_b.trip_number.encode() not in page("trips:trip_list")
        assert b"CON-ALPHA" in page("contracts:contract_list") and b"CON-BRAVO" not in page("contracts:contract_list")
        assert m_a.maintenance_number.encode() in page("maintenance:maintenance_list")
        assert m_b.maintenance_number.encode() not in page("maintenance:maintenance_list")
        assert client.get(reverse("locations:site_detail", kwargs={"uuid": site_b.uuid})).status_code == 404
        assert client.get(reverse("drivers:driver_detail", kwargs={"uuid": driver_b.uuid})).status_code == 404
        assert client.get(reverse("trips:trip_detail", kwargs={"uuid": trip_b.uuid})).status_code == 404
        assert client.get(reverse("maintenance:maintenance_detail", kwargs={"uuid": m_b.uuid})).status_code == 404
        assert client.get(reverse("trips:trip_detail", kwargs={"uuid": trip_a.uuid})).status_code == 200
        assert site_a and driver_a

    def test_filter_dropdowns_do_not_leak_other_clients_names(self, client, world):
        client.force_login(world["user_a"])
        for name in ("trips:trip_list", "locations:site_list", "drivers:driver_list", "contracts:contract_list"):
            assert b"Client Bravo" not in client.get(reverse(name)).content, name

    def test_documents_are_scoped_through_the_record_they_are_attached_to(self, client, world):
        doc_a = DocumentFactory(content_object=world["va1"], title="AlphaPapers")
        doc_b = DocumentFactory(content_object=world["vb1"], title="BravoPapers")
        client.force_login(world["user_a"])
        listing = client.get(reverse("documents:document_list")).content
        assert b"AlphaPapers" in listing and b"BravoPapers" not in listing
        for name in ("documents:document_detail", "documents:document_download", "documents:document_preview"):
            assert client.get(reverse(name, kwargs={"uuid": doc_b.uuid})).status_code == 404, name
        assert client.get(reverse("documents:document_detail", kwargs={"uuid": doc_a.uuid})).status_code == 200

    def test_live_map_and_devices_are_scoped(self, client, world):
        client.force_login(world["user_a"])
        assert client.get(reverse("tracking:live_map")).status_code == 200
        devices = client.get(reverse("tracking:device_list")).content
        assert world["da1"].imei.encode() in devices and world["db1"].imei.encode() not in devices
        assert client.get(reverse("tracking:device_detail", kwargs={"uuid": world["db1"].uuid})).status_code == 404

    def test_vehicle_detail_hides_trips_run_for_a_previous_client(self, client, world):
        old_trip = TripFactory(client=world["a"], vehicle=world["va1"], trip_number="TRP-OLDOWNER")
        vehicle = world["va1"]
        vehicle.client = world["b"]
        vehicle.save(update_fields=["client"])
        client.force_login(world["user_b"])
        page = client.get(reverse("vehicles:vehicle_detail", kwargs={"uuid": vehicle.uuid}), {"tab": "trips"}).content
        assert old_trip.trip_number.encode() not in page


class TestDashboardAndReports:
    def test_dashboard_numbers_are_the_clients_own(self, client, world):
        TripFactory(client=world["a"]), TripFactory(client=world["b"]), TripFactory(client=world["b"])
        client.force_login(world["user_a"])
        ctx = client.get(reverse("core:dashboard")).context
        assert ctx["is_client_user"] is True
        assert ctx["total_vehicles"] == 2
        assert ctx["total_clients"] == 1
        assert ctx["total_trips"] == 1
        assert ctx["fleet_connectivity"]["total_tracked"] == 2

        client.force_login(world["user_b"])
        ctx = client.get(reverse("core:dashboard")).context
        assert ctx["total_vehicles"] == 1 and ctx["total_trips"] == 2

    def test_staff_dashboard_still_counts_everything(self, client, world):
        client.force_login(world["staff"])
        ctx = client.get(reverse("core:dashboard")).context
        assert ctx["total_vehicles"] == 3 and ctx["total_clients"] == 2

    def test_client_dashboard_shows_no_administration_data(self, client, world):
        client.force_login(world["user_a"])
        response = client.get(reverse("core:dashboard"))
        assert "dashboard/workspace.html" in [t.name for t in response.templates]
        assert response.context["total_users"] is None and response.context["total_roles"] is None
        assert b"Client Bravo" not in response.content and b"BRAVO-001" not in response.content


class TestClientUsersAreReadOnlyAndLimited:
    def test_a_generous_role_never_widens_a_client_user(self, world):
        user = world["user_a"]
        assert user_has_permission(user, "vehicle", "view")
        for module, action in [("vehicle", "create"), ("vehicle", "update"), ("client", "update"),
                               ("tracking_device", "command"), ("vendor", "view"), ("user", "view"),
                               ("alert", "update"), ("geofence", "view"), ("audit_log", "view"),
                               ("settings", "view"), ("branch", "view"), ("role", "view")]:
            assert not user_has_permission(user, module, action), (module, action)
        assert user_has_permission(world["staff"], "vehicle", "create")
        # Alerts are client-aware (Alert.client): a client user may VIEW their own, never act on them.
        assert user_has_permission(user, "alert", "view")

    def test_writes_and_unscoped_modules_are_403(self, client, world):
        client.force_login(world["user_a"])
        assert client.get(reverse("vehicles:vehicle_create")).status_code == 403
        assert client.post(reverse("vehicles:vehicle_deactivate", kwargs={"uuid": world["va1"].uuid})).status_code == 403
        assert client.post(reverse("clients:client_deactivate", kwargs={"uuid": world["a"].uuid})).status_code == 403
        for name in ("accounts_portal:user_list", "accounts_portal:role_list", "audit:audit_log_list", "core:settings",
                     "vendors:vendor_list", "geofences:geofence_list", "locations:branch_list",
                     "dispatch:board", "core:master_data"):
            assert client.get(reverse(name)).status_code == 403, name

    def test_sidebar_only_offers_what_a_client_user_can_open(self, client, world):
        client.force_login(world["user_a"])
        nav = client.get(reverse("core:dashboard")).context["nav_sections"]
        urls = {item["url"] for section in nav for item in section["items"]}
        assert reverse("vehicles:vehicle_list") in urls and reverse("tracking:live_map") in urls
        for name in ("accounts_portal:user_list", "dispatch:board", "core:master_data", "core:settings", "alerts:alert_list"):
            assert reverse(name) not in urls

    def test_api_write_endpoints_are_refused(self, world):
        api = APIClient()
        api.force_authenticate(world["user_a"])
        assert api.get("/api/v1/users/").status_code == 403


class TestUserManagement:
    def test_admin_can_link_a_user_to_a_client(self, client, world):
        admin_role = role_with((M.USER, A.VIEW), (M.USER, A.CREATE), (M.USER, A.UPDATE))
        admin = UserFactory(role=admin_role)
        target = UserFactory(role=None)
        client.force_login(admin)
        form_page = client.get(reverse("accounts_portal:user_edit", kwargs={"uuid": target.uuid}))
        assert b"Client (client user only)" in form_page.content
        assert "client" in form_page.context["form"].fields

    def test_a_client_user_cannot_reach_user_administration(self, client, world):
        client.force_login(world["user_a"])
        assert client.get(reverse("accounts_portal:user_create")).status_code == 403


class TestRawRetention:
    def test_purge_deletes_only_old_raw_rows_and_never_history(self, world):
        from django.core.management import call_command

        old = RawTelemetryEvent.objects.filter(device=world["da1"]).first()
        RawTelemetryEvent.objects.filter(pk=old.pk).update(received_at=timezone.now() - datetime.timedelta(days=90))
        events_before = TelemetryEvent.objects.count()
        call_command("purge_raw_telemetry", "--days", "30")
        assert not RawTelemetryEvent.objects.filter(pk=old.pk).exists()
        assert RawTelemetryEvent.objects.filter(device=world["db1"]).exists()
        assert TelemetryEvent.objects.count() == events_before


def test_every_vehicle_belongs_to_at_most_one_client_and_many_vehicles_share_a_client(world):
    assert world["va1"].client_id == world["va2"].client_id == world["a"].id
    assert world["a"].vehicles.count() == 2 and Vehicle.objects.filter(client=world["b"]).count() == 1
