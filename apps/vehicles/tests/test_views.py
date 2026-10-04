import datetime

import pytest
from django.urls import reverse

from apps.accounts.models import Permission, RolePermission
from apps.accounts.tests.factories import RoleFactory, UserFactory
from apps.drivers.tests.factories import DriverFactory
from apps.vehicles.models import Vehicle, VehicleDriverAssignment
from apps.vehicles.tests.factories import FuelTypeFactory, VehicleFactory, VehicleTypeFactory

pytestmark = pytest.mark.django_db


def _role_with(*module_actions):
    role = RoleFactory()
    for module, action in module_actions:
        permission, _ = Permission.objects.get_or_create(module=module, action=action)
        RolePermission.objects.create(role=role, permission=permission)
    return role


class TestVehicleListView:
    def test_anonymous_redirected_to_login(self, client):
        response = client.get(reverse("vehicles:vehicle_list"))
        assert response.status_code == 302

    def test_user_without_permission_gets_403(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("vehicles:vehicle_list"))
        assert response.status_code == 403

    def test_user_with_view_permission_sees_list(self, client):
        role = _role_with((Permission.Module.VEHICLE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        VehicleFactory.create_batch(2)
        client.force_login(viewer)
        response = client.get(reverse("vehicles:vehicle_list"))
        assert response.status_code == 200
        assert b"Vehicles" in response.content

    def test_search_filters_results(self, client):
        role = _role_with((Permission.Module.VEHICLE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        VehicleFactory(registration_number="KA05XY9999")
        client.force_login(viewer)
        response = client.get(reverse("vehicles:vehicle_list"), {"q": "KA05XY9999"})
        assert response.status_code == 200
        assert b"KA05XY9999" in response.content


class TestVehicleCreateView:
    def test_create_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("vehicles:vehicle_create"))
        assert response.status_code == 403

    def test_create_vehicle_success(self, client):
        role = _role_with(
            (Permission.Module.VEHICLE, Permission.Action.VIEW),
            (Permission.Module.VEHICLE, Permission.Action.CREATE),
        )
        actor = UserFactory(role=role)
        vehicle_type = VehicleTypeFactory()
        fuel_type = FuelTypeFactory()
        client.force_login(actor)
        response = client.post(
            reverse("vehicles:vehicle_create"),
            {
                "registration_number": "KA09NEW0001",
                "vehicle_code": "VNEW0001",
                "vehicle_type": vehicle_type.id,
                "fuel_type": fuel_type.id,
                "make": "Mahindra",
                "model": "Bolero",
                "status": Vehicle.Status.ACTIVE,
                "availability_status": Vehicle.AvailabilityStatus.AVAILABLE,
                "odometer_reading": "0",
                "odometer_unit": Vehicle.OdometerUnit.KM,
                "ownership_type": Vehicle.OwnershipType.OWNED,
            },
        )
        assert response.status_code == 302
        assert Vehicle.objects.filter(registration_number="KA09NEW0001").exists()


class TestVehicleAssignDriver:
    def test_assign_driver_via_view_creates_active_assignment(self, client):
        role = _role_with(
            (Permission.Module.VEHICLE, Permission.Action.VIEW),
            (Permission.Module.ASSIGNMENT, Permission.Action.CREATE),
        )
        actor = UserFactory(role=role)
        vehicle = VehicleFactory()
        driver = DriverFactory()
        client.force_login(actor)
        response = client.post(
            reverse("vehicles:vehicle_assign_driver", kwargs={"uuid": vehicle.uuid}),
            {
                "driver": driver.id,
                "assignment_type": VehicleDriverAssignment.AssignmentType.PRIMARY,
                "primary_driver": "on",
                "start_date": datetime.date.today().isoformat(),
                "remarks": "",
            },
        )
        assert response.status_code == 302
        vehicle.refresh_from_db()
        assert vehicle.current_driver_id == driver.id

    def test_assign_driver_requires_permission(self, client):
        actor = UserFactory(role=None)
        vehicle = VehicleFactory()
        driver = DriverFactory()
        client.force_login(actor)
        response = client.post(
            reverse("vehicles:vehicle_assign_driver", kwargs={"uuid": vehicle.uuid}),
            {
                "driver": driver.id,
                "assignment_type": VehicleDriverAssignment.AssignmentType.PRIMARY,
                "primary_driver": "on",
                "start_date": datetime.date.today().isoformat(),
            },
        )
        assert response.status_code == 403


class TestVehicleLifecycleActions:
    def test_deactivate_vehicle(self, client):
        role = _role_with(
            (Permission.Module.VEHICLE, Permission.Action.VIEW),
            (Permission.Module.VEHICLE, Permission.Action.ARCHIVE),
        )
        actor = UserFactory(role=role)
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE)
        client.force_login(actor)
        response = client.post(reverse("vehicles:vehicle_deactivate", kwargs={"uuid": vehicle.uuid}))
        assert response.status_code == 302
        vehicle.refresh_from_db()
        assert vehicle.status == Vehicle.Status.INACTIVE

    def test_deactivate_get_not_allowed(self, client):
        role = _role_with((Permission.Module.VEHICLE, Permission.Action.ARCHIVE))
        actor = UserFactory(role=role)
        vehicle = VehicleFactory()
        client.force_login(actor)
        response = client.get(reverse("vehicles:vehicle_deactivate", kwargs={"uuid": vehicle.uuid}))
        assert response.status_code == 405


class TestVehicleGpsTabTelemetryIntegration:
    """Phase 3.3 integration: the GPS tab must show real telemetry (or a
    real empty state) and device assignment must never touch the vehicle's
    own fleet-lifecycle status fields."""

    def test_no_device_shows_no_device_empty_state(self, client):
        role = _role_with(
            (Permission.Module.VEHICLE, Permission.Action.VIEW),
            (Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW),
        )
        viewer = UserFactory(role=role)
        vehicle = VehicleFactory()
        client.force_login(viewer)
        response = client.get(reverse("vehicles:vehicle_detail", kwargs={"uuid": vehicle.uuid}), {"tab": "gps"})
        assert b"No GPS device linked" in response.content

    def test_device_but_no_telemetry_shows_no_telemetry_empty_state(self, client):
        from apps.tracking.tests.factories import TrackingDeviceFactory

        role = _role_with(
            (Permission.Module.VEHICLE, Permission.Action.VIEW),
            (Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW),
        )
        viewer = UserFactory(role=role)
        vehicle = VehicleFactory()
        TrackingDeviceFactory(vehicle=vehicle)
        client.force_login(viewer)
        response = client.get(reverse("vehicles:vehicle_detail", kwargs={"uuid": vehicle.uuid}), {"tab": "gps"})
        assert b"No telemetry received" in response.content

    def test_real_telemetry_renders_as_data_no_fake_map(self, client):
        from apps.tracking.tests.factories import TrackingDeviceFactory, VehicleCurrentTelemetryFactory

        role = _role_with(
            (Permission.Module.VEHICLE, Permission.Action.VIEW),
            (Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW),
        )
        viewer = UserFactory(role=role)
        vehicle = VehicleFactory()
        device = TrackingDeviceFactory(vehicle=vehicle)
        VehicleCurrentTelemetryFactory(
            vehicle=vehicle, device=device, latitude="12.971600", longitude="77.594600", speed="45.20"
        )
        client.force_login(viewer)
        response = client.get(reverse("vehicles:vehicle_detail", kwargs={"uuid": vehicle.uuid}), {"tab": "gps"})
        assert response.status_code == 200
        assert b"12.971600" in response.content
        assert b"77.594600" in response.content

    def test_open_in_live_tracking_link_when_telemetry_exists(self, client):
        from apps.tracking.tests.factories import TrackingDeviceFactory, VehicleCurrentTelemetryFactory

        role = _role_with(
            (Permission.Module.VEHICLE, Permission.Action.VIEW),
            (Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW),
        )
        viewer = UserFactory(role=role)
        vehicle = VehicleFactory()
        device = TrackingDeviceFactory(vehicle=vehicle)
        VehicleCurrentTelemetryFactory(vehicle=vehicle, device=device)
        client.force_login(viewer)
        response = client.get(reverse("vehicles:vehicle_detail", kwargs={"uuid": vehicle.uuid}), {"tab": "gps"})
        expected_url = f"{reverse('tracking:live_map')}?vehicle={vehicle.uuid}".encode()
        assert expected_url in response.content

    def test_open_in_live_tracking_link_absent_without_telemetry(self, client):
        from apps.tracking.tests.factories import TrackingDeviceFactory

        role = _role_with(
            (Permission.Module.VEHICLE, Permission.Action.VIEW),
            (Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW),
        )
        viewer = UserFactory(role=role)
        vehicle = VehicleFactory()
        TrackingDeviceFactory(vehicle=vehicle)
        client.force_login(viewer)
        response = client.get(reverse("vehicles:vehicle_detail", kwargs={"uuid": vehicle.uuid}), {"tab": "gps"})
        assert b"Open in Live Tracking" not in response.content

    def test_device_assignment_never_changes_vehicle_status(self, client):
        from apps.tracking.tests.factories import TrackingDeviceFactory

        role = _role_with(
            (Permission.Module.TRACKING_DEVICE, Permission.Action.VIEW),
            (Permission.Module.TRACKING_DEVICE, Permission.Action.UPDATE),
        )
        actor = UserFactory(role=role)
        vehicle = VehicleFactory(status=Vehicle.Status.ACTIVE, availability_status=Vehicle.AvailabilityStatus.AVAILABLE)
        device = TrackingDeviceFactory()
        client.force_login(actor)

        client.post(reverse("tracking:device_assign", kwargs={"uuid": device.uuid}), {"vehicle": vehicle.id})

        vehicle.refresh_from_db()
        assert vehicle.status == Vehicle.Status.ACTIVE
        assert vehicle.availability_status == Vehicle.AvailabilityStatus.AVAILABLE


class TestVehicleTypeListView:
    def test_anonymous_redirected_to_login(self, client):
        response = client.get(reverse("vehicles:vehicle_type_list"))
        assert response.status_code == 302

    def test_user_without_permission_gets_403(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("vehicles:vehicle_type_list"))
        assert response.status_code == 403

    def test_user_with_view_permission_sees_list(self, client):
        role = _role_with((Permission.Module.VEHICLE_TYPE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        VehicleTypeFactory.create_batch(2)
        client.force_login(viewer)
        response = client.get(reverse("vehicles:vehicle_type_list"))
        assert response.status_code == 200
        assert b"Vehicle Types" in response.content

    def test_search_filters_results(self, client):
        role = _role_with((Permission.Module.VEHICLE_TYPE, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        VehicleTypeFactory(name="Northgate Sedan", code="NGS01")
        client.force_login(viewer)
        response = client.get(reverse("vehicles:vehicle_type_list"), {"q": "Northgate"})
        assert response.status_code == 200
        assert b"Northgate Sedan" in response.content


class TestVehicleTypeCreateView:
    def test_create_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("vehicles:vehicle_type_create"))
        assert response.status_code == 403

    def test_create_vehicle_type_success(self, client):
        role = _role_with(
            (Permission.Module.VEHICLE_TYPE, Permission.Action.VIEW),
            (Permission.Module.VEHICLE_TYPE, Permission.Action.CREATE),
        )
        actor = UserFactory(role=role)
        client.force_login(actor)
        response = client.post(
            reverse("vehicles:vehicle_type_create"),
            {"code": "VTNEW01", "name": "New Type", "is_active": "on"},
        )
        assert response.status_code == 302
        from apps.vehicles.models import VehicleType

        assert VehicleType.objects.filter(code="VTNEW01").exists()

    def test_create_without_name_rejected(self, client):
        role = _role_with(
            (Permission.Module.VEHICLE_TYPE, Permission.Action.VIEW),
            (Permission.Module.VEHICLE_TYPE, Permission.Action.CREATE),
        )
        actor = UserFactory(role=role)
        client.force_login(actor)
        response = client.post(reverse("vehicles:vehicle_type_create"), {"code": "VTNONAME"})
        assert response.status_code == 200
        from apps.vehicles.models import VehicleType

        assert not VehicleType.objects.filter(code="VTNONAME").exists()


class TestVehicleTypeLifecycleActions:
    def test_deactivate_and_activate(self, client):
        role = _role_with(
            (Permission.Module.VEHICLE_TYPE, Permission.Action.VIEW),
            (Permission.Module.VEHICLE_TYPE, Permission.Action.ARCHIVE),
        )
        actor = UserFactory(role=role)
        vehicle_type = VehicleTypeFactory(is_active=True)
        client.force_login(actor)

        response = client.post(reverse("vehicles:vehicle_type_deactivate", kwargs={"uuid": vehicle_type.uuid}))
        assert response.status_code == 302
        vehicle_type.refresh_from_db()
        assert vehicle_type.is_active is False

        response = client.post(reverse("vehicles:vehicle_type_activate", kwargs={"uuid": vehicle_type.uuid}))
        assert response.status_code == 302
        vehicle_type.refresh_from_db()
        assert vehicle_type.is_active is True


class TestAssignmentListView:
    def test_anonymous_redirected_to_login(self, client):
        response = client.get(reverse("vehicles:assignment_list"))
        assert response.status_code == 302

    def test_user_without_permission_gets_403(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("vehicles:assignment_list"))
        assert response.status_code == 403

    def test_user_with_view_permission_sees_list(self, client):
        role = _role_with((Permission.Module.ASSIGNMENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        vehicle = VehicleFactory()
        driver = DriverFactory()
        VehicleDriverAssignment.objects.create(
            vehicle=vehicle, driver=driver, start_date=datetime.date.today(),
            status=VehicleDriverAssignment.Status.ACTIVE,
        )
        client.force_login(viewer)
        response = client.get(reverse("vehicles:assignment_list"))
        assert response.status_code == 200
        assert vehicle.registration_number.encode() in response.content

    def test_search_by_driver_name(self, client):
        role = _role_with((Permission.Module.ASSIGNMENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        vehicle = VehicleFactory()
        driver = DriverFactory(first_name="Zendaya", last_name="Coleman")
        VehicleDriverAssignment.objects.create(
            vehicle=vehicle, driver=driver, start_date=datetime.date.today(),
            status=VehicleDriverAssignment.Status.ACTIVE,
        )
        client.force_login(viewer)
        response = client.get(reverse("vehicles:assignment_list"), {"q": "Zendaya"})
        assert response.status_code == 200
        assert b"Zendaya" in response.content

    def test_status_filter(self, client):
        role = _role_with((Permission.Module.ASSIGNMENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        vehicle1, vehicle2 = VehicleFactory(), VehicleFactory()
        driver1, driver2 = DriverFactory(), DriverFactory()
        VehicleDriverAssignment.objects.create(
            vehicle=vehicle1, driver=driver1, start_date=datetime.date.today(),
            status=VehicleDriverAssignment.Status.ACTIVE,
        )
        VehicleDriverAssignment.objects.create(
            vehicle=vehicle2, driver=driver2, start_date=datetime.date.today(),
            status=VehicleDriverAssignment.Status.ENDED,
        )
        client.force_login(viewer)
        response = client.get(reverse("vehicles:assignment_list"), {"status": VehicleDriverAssignment.Status.ENDED})
        assert vehicle2.registration_number.encode() in response.content
        assert vehicle1.registration_number.encode() not in response.content
