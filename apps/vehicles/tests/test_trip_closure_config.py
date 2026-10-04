"""Trip Closure Time on the Vehicle Create / Edit screens: stored per vehicle,
shown when editing, validated as a whole number of minutes (1–1440), audited,
and backward compatible with requests that don't send the field."""

import pytest
from django.urls import reverse

from apps.accounts.models import Permission
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditLog
from apps.core.tests.factories import role_with
from apps.vehicles.models import Vehicle
from apps.vehicles.tests.factories import FuelTypeFactory, VehicleFactory, VehicleTypeFactory

pytestmark = pytest.mark.django_db


def _manager(client):
    client.force_login(UserFactory(role=role_with(
        (Permission.Module.VEHICLE, Permission.Action.VIEW),
        (Permission.Module.VEHICLE, Permission.Action.CREATE),
        (Permission.Module.VEHICLE, Permission.Action.UPDATE),
    )))


def _create_payload(**overrides):
    data = {
        "registration_number": "KA09TC0001",
        "vehicle_code": "VTC0001",
        "vehicle_type": VehicleTypeFactory().id,
        "fuel_type": FuelTypeFactory().id,
        "make": "Tata",
        "model": "Ace",
        "status": Vehicle.Status.ACTIVE,
        "availability_status": Vehicle.AvailabilityStatus.AVAILABLE,
        "odometer_reading": "0",
        "odometer_unit": Vehicle.OdometerUnit.KM,
        "ownership_type": Vehicle.OwnershipType.OWNED,
    }
    data.update(overrides)
    return data


def _edit_payload(vehicle, **overrides):
    data = {
        "registration_number": vehicle.registration_number,
        "vehicle_code": vehicle.vehicle_code,
        "vehicle_type": vehicle.vehicle_type_id,
        "fuel_type": vehicle.fuel_type_id,
        "make": vehicle.make,
        "model": vehicle.model,
        "status": vehicle.status,
        "availability_status": vehicle.availability_status,
        "odometer_reading": str(vehicle.odometer_reading),
        "odometer_unit": vehicle.odometer_unit,
        "ownership_type": vehicle.ownership_type,
    }
    data.update(overrides)
    return data


class TestCreate:
    def test_create_screen_offers_the_field_prefilled_with_five(self, client):
        _manager(client)
        html = client.get(reverse("vehicles:vehicle_create")).content.decode()
        assert "Trip Closure Time" in html
        assert 'name="trip_closure_minutes"' in html and 'value="5"' in html

    def test_create_with_a_custom_value_is_stored(self, client):
        _manager(client)
        response = client.post(reverse("vehicles:vehicle_create"), _create_payload(trip_closure_minutes="10"))
        assert response.status_code == 302
        assert Vehicle.objects.get(registration_number="KA09TC0001").trip_closure_minutes == 10

    def test_create_without_the_field_defaults_to_five(self, client):
        _manager(client)
        response = client.post(reverse("vehicles:vehicle_create"), _create_payload())
        assert response.status_code == 302
        assert Vehicle.objects.get(registration_number="KA09TC0001").trip_closure_minutes == 5

    @pytest.mark.parametrize("bad", ["", "0", "-5", "7.5", "ten", "1441"])
    def test_invalid_values_are_rejected(self, client, bad):
        _manager(client)
        response = client.post(reverse("vehicles:vehicle_create"), _create_payload(trip_closure_minutes=bad))
        assert response.status_code == 200  # form re-rendered with the error
        assert "trip_closure_minutes" in response.context["form"].errors
        assert not Vehicle.objects.filter(registration_number="KA09TC0001").exists()


class TestEdit:
    def test_edit_screen_shows_the_current_value(self, client):
        vehicle = VehicleFactory(trip_closure_minutes=15)
        _manager(client)
        html = client.get(reverse("vehicles:vehicle_edit", kwargs={"uuid": vehicle.uuid})).content.decode()
        assert 'name="trip_closure_minutes"' in html and 'value="15"' in html

    def test_edit_changes_only_that_vehicle_and_is_audited(self, client):
        edited, other = VehicleFactory(trip_closure_minutes=5), VehicleFactory(trip_closure_minutes=10)
        _manager(client)
        response = client.post(
            reverse("vehicles:vehicle_edit", kwargs={"uuid": edited.uuid}), _edit_payload(edited, trip_closure_minutes="20")
        )
        assert response.status_code == 302
        edited.refresh_from_db()
        other.refresh_from_db()
        assert edited.trip_closure_minutes == 20
        assert other.trip_closure_minutes == 10
        log = AuditLog.objects.filter(entity_id=str(edited.pk), action=AuditLog.Action.UPDATE).latest("id")
        assert log.old_value["trip_closure_minutes"] == 5 and log.new_value["trip_closure_minutes"] == 20

    def test_edit_without_the_field_keeps_the_configured_value(self, client):
        vehicle = VehicleFactory(trip_closure_minutes=15)
        _manager(client)
        response = client.post(
            reverse("vehicles:vehicle_edit", kwargs={"uuid": vehicle.uuid}), _edit_payload(vehicle, make="Ashok")
        )
        assert response.status_code == 302
        vehicle.refresh_from_db()
        assert vehicle.make == "Ashok" and vehicle.trip_closure_minutes == 15

    def test_blank_value_on_edit_is_rejected_not_reset(self, client):
        vehicle = VehicleFactory(trip_closure_minutes=15)
        _manager(client)
        response = client.post(
            reverse("vehicles:vehicle_edit", kwargs={"uuid": vehicle.uuid}), _edit_payload(vehicle, trip_closure_minutes="")
        )
        assert response.status_code == 200
        vehicle.refresh_from_db()
        assert vehicle.trip_closure_minutes == 15


def test_detail_page_shows_the_configured_value(client):
    vehicle = VehicleFactory(trip_closure_minutes=12)
    _manager(client)
    html = client.get(reverse("vehicles:vehicle_detail", kwargs={"uuid": vehicle.uuid})).content.decode()
    assert "Trip Closure Time" in html and "12 min" in html
