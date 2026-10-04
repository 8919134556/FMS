"""End-to-end smoke test for the Documents module: upload -> attach to each
of the 6 documentable entities -> the entity's own Documents tab shows it
-> replace/version -> archive/restore -> Dashboard KPIs. Mirrors the smoke
tests for every other module — the quality benchmark this one was built
to match.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import Permission, RolePermission
from apps.accounts.tests.factories import RoleFactory, UserFactory
from apps.clients.tests.factories import ClientFactory
from apps.documents.models import Document
from apps.documents.tests.factories import make_pdf
from apps.drivers.tests.factories import DriverFactory
from apps.locations.tests.factories import SiteFactory
from apps.maintenance.tests.factories import MaintenanceFactory
from apps.trips.tests.factories import TripFactory
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


def _full_access_role():
    role = RoleFactory()
    for module in [
        Permission.Module.DOCUMENT, Permission.Module.VEHICLE, Permission.Module.DRIVER,
        Permission.Module.CLIENT, Permission.Module.SITE, Permission.Module.TRIP, Permission.Module.MAINTENANCE,
    ]:
        for action in [
            Permission.Action.VIEW, Permission.Action.CREATE, Permission.Action.UPDATE,
            Permission.Action.ARCHIVE, Permission.Action.DELETE, Permission.Action.EXPORT,
        ]:
            permission, _ = Permission.objects.get_or_create(module=module, action=action)
            RolePermission.objects.create(role=role, permission=permission)
    return role


ENTITY_DETAIL_URL_NAMES = {
    "client": "clients:client_detail",
    "site": "locations:site_detail",
    "vehicle": "vehicles:vehicle_detail",
    "driver": "drivers:driver_detail",
    "trip": "trips:trip_detail",
    "maintenance": "maintenance:maintenance_detail",
}


class TestDocumentModuleSmoke:
    def test_upload_and_attach_to_every_documentable_entity(self, client):
        role = _full_access_role()
        actor = UserFactory(role=role)
        client.force_login(actor)

        entities = {
            "client": ClientFactory(),
            "site": SiteFactory(),
            "vehicle": VehicleFactory(),
            "driver": DriverFactory(),
            "trip": TripFactory(),
            "maintenance": MaintenanceFactory(),
        }

        for entity_type, obj in entities.items():
            create_response = client.post(
                reverse("documents:document_create"),
                {
                    "document_type": Document.DocumentType.OTHER,
                    "title": f"{entity_type.title()} Document",
                    "description": "",
                    "file": make_pdf(f"{entity_type}.pdf"),
                    "notes": "",
                    "entity_type": entity_type,
                    "object_id": obj.pk,
                },
            )
            assert create_response.status_code == 302, f"{entity_type} upload failed"
            doc = Document.objects.get(title=f"{entity_type.title()} Document")
            assert doc.content_object == obj

            entity_detail_response = client.get(
                reverse(ENTITY_DETAIL_URL_NAMES[entity_type], kwargs={"uuid": obj.uuid}), {"tab": "documents"}
            )
            assert entity_detail_response.status_code == 200
            assert doc.document_number.encode() in entity_detail_response.content
            assert f"{entity_type.title()} Document".encode() in entity_detail_response.content

    def test_full_document_lifecycle(self, client):
        role = _full_access_role()
        actor = UserFactory(role=role)
        client.force_login(actor)
        vehicle = VehicleFactory()

        create_response = client.post(
            reverse("documents:document_create"),
            {
                "document_type": Document.DocumentType.INSURANCE,
                "title": "Vehicle Insurance",
                "description": "Comprehensive cover",
                "file": make_pdf("insurance.pdf"),
                "notes": "",
                "entity_type": "vehicle",
                "object_id": vehicle.id,
            },
        )
        assert create_response.status_code == 302
        doc = Document.objects.get(title="Vehicle Insurance")

        detail_response = client.get(reverse("documents:document_detail", kwargs={"uuid": doc.uuid}))
        assert detail_response.status_code == 200

        replace_response = client.post(
            reverse("documents:document_replace", kwargs={"uuid": doc.uuid}), {"file": make_pdf("insurance_v2.pdf")}
        )
        assert replace_response.status_code == 302
        new_doc = Document.objects.get(replaces=doc)
        assert new_doc.version == 2

        archive_response = client.post(reverse("documents:document_archive", kwargs={"uuid": new_doc.uuid}))
        assert archive_response.status_code == 302
        new_doc.refresh_from_db()
        assert new_doc.status == Document.Status.ARCHIVED

        restore_response = client.post(reverse("documents:document_restore", kwargs={"uuid": new_doc.uuid}))
        assert restore_response.status_code == 302
        new_doc.refresh_from_db()
        assert new_doc.status == Document.Status.ACTIVE

        download_response = client.get(reverse("documents:document_download", kwargs={"uuid": new_doc.uuid}))
        assert download_response.status_code == 200

        list_response = client.get(reverse("documents:document_list"))
        assert list_response.status_code == 200

        export_response = client.get(reverse("documents:document_export"))
        assert export_response.status_code == 200
        assert export_response["Content-Type"] == "text/csv"

    def test_dashboard_document_kpis_and_widget(self, client):
        import datetime

        from django.utils import timezone

        from apps.documents.tests.factories import DocumentFactory

        role = _full_access_role()
        actor = UserFactory(role=role)
        DocumentFactory(
            title="Expiring Soon Doc", expiry_date=timezone.now().date() + datetime.timedelta(days=5),
            content_object=VehicleFactory(),
        )
        client.force_login(actor)

        response = client.get(reverse("core:dashboard"))
        assert response.status_code == 200
        assert b"Total Documents" in response.content
        assert b"Documents Expiring Soon" in response.content
        assert b"Expiring Soon Doc" in response.content

    def test_dashboard_with_no_documents(self, client):
        role = _full_access_role()
        actor = UserFactory(role=role)
        client.force_login(actor)
        response = client.get(reverse("core:dashboard"))
        assert response.status_code == 200
        assert b"No documents are expiring soon" in response.content
