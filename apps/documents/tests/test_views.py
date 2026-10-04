import pytest
from django.urls import reverse

from apps.accounts.models import Permission, RolePermission
from apps.accounts.tests.factories import RoleFactory, UserFactory
from apps.documents.models import Document
from apps.documents.tests.factories import DocumentFactory, make_pdf
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


def _role_with(*module_actions):
    role = RoleFactory()
    for module, action in module_actions:
        permission, _ = Permission.objects.get_or_create(module=module, action=action)
        RolePermission.objects.create(role=role, permission=permission)
    return role


def _full_document_role():
    return _role_with(
        (Permission.Module.DOCUMENT, Permission.Action.VIEW),
        (Permission.Module.DOCUMENT, Permission.Action.CREATE),
        (Permission.Module.DOCUMENT, Permission.Action.UPDATE),
        (Permission.Module.DOCUMENT, Permission.Action.ARCHIVE),
        (Permission.Module.DOCUMENT, Permission.Action.DELETE),
        (Permission.Module.DOCUMENT, Permission.Action.EXPORT),
    )


class TestDocumentListView:
    def test_anonymous_redirected_to_login(self, client):
        response = client.get(reverse("documents:document_list"))
        assert response.status_code == 302

    def test_user_without_permission_gets_403(self, client):
        viewer = UserFactory(role=None)
        client.force_login(viewer)
        response = client.get(reverse("documents:document_list"))
        assert response.status_code == 403

    def test_user_with_view_permission_sees_list(self, client):
        role = _role_with((Permission.Module.DOCUMENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        DocumentFactory.create_batch(2)
        client.force_login(viewer)
        response = client.get(reverse("documents:document_list"))
        assert response.status_code == 200
        assert b"Documents" in response.content

    def test_search_by_document_number(self, client):
        role = _role_with((Permission.Module.DOCUMENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        DocumentFactory(document_number="DOC-777777")
        client.force_login(viewer)
        response = client.get(reverse("documents:document_list"), {"q": "DOC-777777"})
        assert response.status_code == 200
        assert b"DOC-777777" in response.content

    def test_entity_type_filter(self, client):
        from apps.clients.tests.factories import ClientFactory

        role = _role_with((Permission.Module.DOCUMENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        vehicle_doc = DocumentFactory(document_number="DOC-888801", content_object=VehicleFactory())
        DocumentFactory(document_number="DOC-888802", content_object=ClientFactory())
        client.force_login(viewer)
        response = client.get(reverse("documents:document_list"), {"entity_type": "vehicle"})
        assert b"DOC-888801" in response.content
        assert b"DOC-888802" not in response.content

    def test_expiry_filter_expired(self, client):
        import datetime

        from django.utils import timezone

        role = _role_with((Permission.Module.DOCUMENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        DocumentFactory(document_number="DOC-EXPIRED1", expiry_date=timezone.now().date() - datetime.timedelta(days=2))
        DocumentFactory(document_number="DOC-VALID001", expiry_date=timezone.now().date() + datetime.timedelta(days=200))
        client.force_login(viewer)
        response = client.get(reverse("documents:document_list"), {"expiry": "expired"})
        assert b"DOC-EXPIRED1" in response.content
        assert b"DOC-VALID001" not in response.content

    def test_pagination(self, client):
        role = _role_with((Permission.Module.DOCUMENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        DocumentFactory.create_batch(30)
        client.force_login(viewer)
        response = client.get(reverse("documents:document_list"))
        assert response.status_code == 200
        assert response.context["page_obj"].has_next()

    def test_export_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("documents:document_export"))
        assert response.status_code == 403

    def test_export_returns_csv(self, client):
        role = _role_with((Permission.Module.DOCUMENT, Permission.Action.EXPORT))
        actor = UserFactory(role=role)
        DocumentFactory(document_number="DOC-999901")
        client.force_login(actor)
        response = client.get(reverse("documents:document_export"))
        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv"
        assert b"DOC-999901" in response.content

    def test_empty_state_no_documents(self, client):
        role = _role_with((Permission.Module.DOCUMENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        client.force_login(viewer)
        response = client.get(reverse("documents:document_list"))
        assert b"No documents yet" in response.content


class TestDocumentCreateView:
    def test_create_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("documents:document_create"))
        assert response.status_code == 403

    def test_create_success(self, client):
        role = _full_document_role()
        actor = UserFactory(role=role)
        vehicle = VehicleFactory()
        client.force_login(actor)
        response = client.post(
            reverse("documents:document_create"),
            {
                "document_type": Document.DocumentType.RC,
                "title": "Registration Certificate",
                "description": "",
                "file": make_pdf("rc.pdf"),
                "notes": "",
                "entity_type": "vehicle",
                "object_id": vehicle.id,
            },
        )
        assert response.status_code == 302
        doc = Document.objects.get(title="Registration Certificate")
        assert doc.content_object == vehicle

    def test_create_prefills_locked_entity_from_query_params(self, client):
        role = _full_document_role()
        RolePermission.objects.create(
            role=role, permission=Permission.objects.get_or_create(module=Permission.Module.VEHICLE, action=Permission.Action.VIEW)[0]
        )
        actor = UserFactory(role=role)
        vehicle = VehicleFactory()
        client.force_login(actor)
        response = client.get(reverse("documents:document_create"), {"entity_type": "vehicle", "object_id": vehicle.id})
        assert response.status_code == 200
        assert str(vehicle).encode() in response.content

    def test_locked_entity_not_resolved_without_that_modules_view_permission(self, client):
        role = _full_document_role()  # document perms only, no vehicle.view
        actor = UserFactory(role=role)
        vehicle = VehicleFactory(registration_number="KA05NOPE001")
        client.force_login(actor)
        response = client.get(reverse("documents:document_create"), {"entity_type": "vehicle", "object_id": vehicle.id})
        assert response.status_code == 200
        assert b"KA05NOPE001" not in response.content

    def test_create_rejects_dangerous_extension(self, client):
        role = _full_document_role()
        actor = UserFactory(role=role)
        vehicle = VehicleFactory()
        client.force_login(actor)
        malicious = make_pdf("virus.exe", content=b"MZ\x90\x00fake exe")
        response = client.post(
            reverse("documents:document_create"),
            {
                "document_type": Document.DocumentType.OTHER,
                "title": "Bad File",
                "file": malicious,
                "entity_type": "vehicle",
                "object_id": vehicle.id,
            },
        )
        assert response.status_code == 200
        assert not Document.objects.filter(title="Bad File").exists()

    def test_create_rejects_invalid_related_record(self, client):
        role = _full_document_role()
        actor = UserFactory(role=role)
        client.force_login(actor)
        response = client.post(
            reverse("documents:document_create"),
            {
                "document_type": Document.DocumentType.OTHER,
                "title": "Orphan Doc",
                "file": make_pdf(),
                "entity_type": "vehicle",
                "object_id": 999999,
            },
        )
        assert response.status_code == 200
        assert not Document.objects.filter(title="Orphan Doc").exists()


class TestDocumentDetailView:
    def test_detail_requires_permission(self, client):
        actor = UserFactory(role=None)
        doc = DocumentFactory()
        client.force_login(actor)
        response = client.get(reverse("documents:document_detail", kwargs={"uuid": doc.uuid}))
        assert response.status_code == 403

    def test_detail_renders(self, client):
        role = _role_with((Permission.Module.DOCUMENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        doc = DocumentFactory(title="My Insurance Doc")
        client.force_login(viewer)
        response = client.get(reverse("documents:document_detail", kwargs={"uuid": doc.uuid}))
        assert response.status_code == 200
        assert b"My Insurance Doc" in response.content


class TestDocumentEditView:
    def test_edit_updates_metadata(self, client):
        role = _full_document_role()
        actor = UserFactory(role=role)
        doc = DocumentFactory(title="Old Title")
        client.force_login(actor)
        response = client.post(
            reverse("documents:document_edit", kwargs={"uuid": doc.uuid}),
            {"document_type": doc.document_type, "title": "New Title", "description": ""},
        )
        assert response.status_code == 302
        doc.refresh_from_db()
        assert doc.title == "New Title"


class TestDocumentArchiveRestore:
    def test_archive_success(self, client):
        role = _full_document_role()
        actor = UserFactory(role=role)
        doc = DocumentFactory(status=Document.Status.ACTIVE)
        client.force_login(actor)
        response = client.post(reverse("documents:document_archive", kwargs={"uuid": doc.uuid}))
        assert response.status_code == 302
        doc.refresh_from_db()
        assert doc.status == Document.Status.ARCHIVED

    def test_archive_requires_permission(self, client):
        actor = UserFactory(role=None)
        doc = DocumentFactory()
        client.force_login(actor)
        response = client.post(reverse("documents:document_archive", kwargs={"uuid": doc.uuid}))
        assert response.status_code == 403

    def test_restore_success(self, client):
        role = _full_document_role()
        actor = UserFactory(role=role)
        doc = DocumentFactory(status=Document.Status.ARCHIVED)
        client.force_login(actor)
        response = client.post(reverse("documents:document_restore", kwargs={"uuid": doc.uuid}))
        assert response.status_code == 302
        doc.refresh_from_db()
        assert doc.status == Document.Status.ACTIVE


class TestDocumentReplace:
    def test_replace_uploads_new_version(self, client):
        role = _full_document_role()
        actor = UserFactory(role=role)
        doc = DocumentFactory(version=1)
        client.force_login(actor)
        response = client.post(
            reverse("documents:document_replace", kwargs={"uuid": doc.uuid}),
            {"file": make_pdf("v2.pdf")},
        )
        assert response.status_code == 302
        doc.refresh_from_db()
        assert doc.is_current_version is False
        assert Document.objects.filter(replaces=doc, version=2).exists()


class TestDocumentDelete:
    def test_delete_requires_permission(self, client):
        role = _role_with(
            (Permission.Module.DOCUMENT, Permission.Action.VIEW), (Permission.Module.DOCUMENT, Permission.Action.UPDATE)
        )
        actor = UserFactory(role=role)
        doc = DocumentFactory()
        client.force_login(actor)
        response = client.post(reverse("documents:document_delete", kwargs={"uuid": doc.uuid}))
        assert response.status_code == 403

    def test_delete_success(self, client):
        role = _full_document_role()
        actor = UserFactory(role=role)
        doc = DocumentFactory()
        doc_id = doc.pk
        client.force_login(actor)
        response = client.post(reverse("documents:document_delete", kwargs={"uuid": doc.uuid}))
        assert response.status_code == 302
        assert not Document.objects.filter(pk=doc_id).exists()


class TestDocumentDownloadSecurity:
    """The security-critical suite the spec explicitly calls out."""

    def test_authenticated_user_with_permission_can_download(self, client):
        role = _role_with((Permission.Module.DOCUMENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        doc = DocumentFactory()
        client.force_login(viewer)
        response = client.get(reverse("documents:document_download", kwargs={"uuid": doc.uuid}))
        assert response.status_code == 200
        assert response["Content-Disposition"].startswith("attachment")
        assert doc.original_filename in response["Content-Disposition"]

    def test_authenticated_user_without_permission_denied(self, client):
        viewer = UserFactory(role=None)
        doc = DocumentFactory()
        client.force_login(viewer)
        response = client.get(reverse("documents:document_download", kwargs={"uuid": doc.uuid}))
        assert response.status_code == 403

    def test_unauthenticated_user_denied(self, client):
        doc = DocumentFactory()
        response = client.get(reverse("documents:document_download", kwargs={"uuid": doc.uuid}))
        assert response.status_code == 302
        assert reverse("accounts:login") in response.url

    def test_nonexistent_document_returns_404(self, client):
        import uuid as uuid_lib

        role = _role_with((Permission.Module.DOCUMENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        client.force_login(viewer)
        response = client.get(reverse("documents:document_download", kwargs={"uuid": uuid_lib.uuid4()}))
        assert response.status_code == 404

    def test_archived_document_still_downloadable_by_permitted_user(self, client):
        role = _role_with((Permission.Module.DOCUMENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        doc = DocumentFactory(status=Document.Status.ARCHIVED)
        client.force_login(viewer)
        response = client.get(reverse("documents:document_download", kwargs={"uuid": doc.uuid}))
        assert response.status_code == 200

    def test_deleted_document_returns_404(self, client):
        from apps.documents import services

        role = _role_with((Permission.Module.DOCUMENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        doc = DocumentFactory()
        doc_uuid = doc.uuid
        services.delete_document(document=doc, deleted_by=viewer)
        client.force_login(viewer)
        response = client.get(reverse("documents:document_download", kwargs={"uuid": doc_uuid}))
        assert response.status_code == 404

    def test_download_is_logged_in_audit_trail(self, client):
        from apps.audit.models import AuditLog

        role = _role_with((Permission.Module.DOCUMENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        doc = DocumentFactory()
        client.force_login(viewer)
        client.get(reverse("documents:document_download", kwargs={"uuid": doc.uuid}))
        assert AuditLog.objects.filter(action=AuditLog.Action.DOWNLOAD, entity_id=str(doc.pk)).exists()

    def test_download_url_does_not_expose_raw_media_path(self, client):
        role = _role_with((Permission.Module.DOCUMENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        doc = DocumentFactory()
        client.force_login(viewer)
        response = client.get(reverse("documents:document_detail", kwargs={"uuid": doc.uuid}))
        assert response.status_code == 200
        assert b"/media/" not in response.content


class TestDocumentPreview:
    def test_preview_requires_permission(self, client):
        actor = UserFactory(role=None)
        doc = DocumentFactory()
        client.force_login(actor)
        response = client.get(reverse("documents:document_preview", kwargs={"uuid": doc.uuid}))
        assert response.status_code == 403

    def test_preview_inline_for_pdf(self, client):
        role = _role_with((Permission.Module.DOCUMENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        doc = DocumentFactory(original_filename="report.pdf")
        client.force_login(viewer)
        response = client.get(reverse("documents:document_preview", kwargs={"uuid": doc.uuid}))
        assert response.status_code == 200
        assert response["Content-Disposition"] == "inline"

    def test_preview_404_for_non_previewable_type(self, client):
        role = _role_with((Permission.Module.DOCUMENT, Permission.Action.VIEW))
        viewer = UserFactory(role=role)
        doc = DocumentFactory(original_filename="spreadsheet.xlsx")
        client.force_login(viewer)
        response = client.get(reverse("documents:document_preview", kwargs={"uuid": doc.uuid}))
        assert response.status_code == 404


class TestRelatedOptionsEndpoint:
    def test_returns_matching_options(self, client):
        role = _role_with(
            (Permission.Module.DOCUMENT, Permission.Action.VIEW),
            (Permission.Module.VEHICLE, Permission.Action.VIEW),
        )
        viewer = UserFactory(role=role)
        vehicle = VehicleFactory(registration_number="KA05SEARCH1")
        client.force_login(viewer)
        response = client.get(reverse("documents:document_related_options"), {"entity_type": "vehicle", "q": "KA05SEARCH1"})
        assert response.status_code == 200
        assert b"KA05SEARCH1" in response.content

    def test_requires_permission(self, client):
        actor = UserFactory(role=None)
        client.force_login(actor)
        response = client.get(reverse("documents:document_related_options"), {"entity_type": "vehicle"})
        assert response.status_code == 403

    def test_does_not_list_records_of_a_module_the_viewer_cannot_open(self, client):
        role = _role_with((Permission.Module.DOCUMENT, Permission.Action.VIEW))  # no vehicle.view
        viewer = UserFactory(role=role)
        VehicleFactory(registration_number="KA05HIDDEN1")
        client.force_login(viewer)
        response = client.get(reverse("documents:document_related_options"), {"entity_type": "vehicle", "q": "KA05HIDDEN1"})
        assert response.status_code == 200
        assert b"KA05HIDDEN1" not in response.content
