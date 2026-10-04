import pytest
from django.core.exceptions import ValidationError

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditLog
from apps.documents import services
from apps.documents.models import Document
from apps.documents.tests.factories import DocumentFactory, make_pdf
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


class TestGenerateDocumentNumber:
    def test_generates_sequential_numbers(self):
        first = services.generate_document_number()
        second = services.generate_document_number()
        assert first != second
        assert first.startswith("DOC-")
        assert int(second.split("-")[1]) == int(first.split("-")[1]) + 1


class TestUploadDocument:
    def test_upload_creates_document_with_metadata(self):
        vehicle = VehicleFactory()
        actor = UserFactory()
        doc = services.upload_document(
            content_object=vehicle, title="Insurance Policy", document_type=Document.DocumentType.INSURANCE,
            description="Annual policy", file=make_pdf("insurance.pdf"), issue_date=None, expiry_date=None,
            notes="", uploaded_by=actor,
        )
        assert doc.document_number.startswith("DOC-")
        assert doc.original_filename == "insurance.pdf"
        assert doc.file_size > 0
        assert doc.mime_type == "application/pdf"
        assert doc.content_object == vehicle
        assert doc.created_by == actor
        assert AuditLog.objects.filter(action=AuditLog.Action.CREATE, module="document", entity_id=str(doc.pk)).exists()

    def test_stored_filename_is_not_the_original(self):
        vehicle = VehicleFactory()
        actor = UserFactory()
        doc = services.upload_document(
            content_object=vehicle, title="Doc", document_type=Document.DocumentType.OTHER,
            description="", file=make_pdf("secret_original_name.pdf"), issue_date=None, expiry_date=None,
            notes="", uploaded_by=actor,
        )
        assert "secret_original_name" not in doc.file.name
        assert doc.original_filename == "secret_original_name.pdf"


class TestReplaceDocument:
    def test_replace_creates_new_version(self):
        original = DocumentFactory(version=1)
        actor = UserFactory()
        new_doc = services.replace_document(document=original, file=make_pdf("v2.pdf"), replaced_by=actor)
        assert new_doc.version == 2
        assert new_doc.replaces_id == original.pk
        assert new_doc.content_object == original.content_object
        original.refresh_from_db()
        assert original.is_current_version is False
        assert new_doc.is_current_version is True

    def test_cannot_replace_an_already_superseded_document(self):
        original = DocumentFactory(version=1)
        actor = UserFactory()
        services.replace_document(document=original, file=make_pdf("v2.pdf"), replaced_by=actor)
        original.refresh_from_db()
        with pytest.raises(ValidationError, match="current version"):
            services.replace_document(document=original, file=make_pdf("v3.pdf"), replaced_by=actor)


class TestArchiveRestore:
    def test_archive_success(self):
        doc = DocumentFactory(status=Document.Status.ACTIVE)
        actor = UserFactory()
        services.archive_document(document=doc, archived_by=actor)
        doc.refresh_from_db()
        assert doc.status == Document.Status.ARCHIVED

    def test_archive_already_archived_rejected(self):
        doc = DocumentFactory(status=Document.Status.ARCHIVED)
        actor = UserFactory()
        with pytest.raises(ValidationError):
            services.archive_document(document=doc, archived_by=actor)

    def test_restore_success(self):
        doc = DocumentFactory(status=Document.Status.ARCHIVED)
        actor = UserFactory()
        services.restore_document(document=doc, restored_by=actor)
        doc.refresh_from_db()
        assert doc.status == Document.Status.ACTIVE

    def test_restore_active_document_rejected(self):
        doc = DocumentFactory(status=Document.Status.ACTIVE)
        actor = UserFactory()
        with pytest.raises(ValidationError):
            services.restore_document(document=doc, restored_by=actor)


class TestDeleteDocument:
    def test_delete_soft_deletes_and_hides_from_default_manager(self):
        doc = DocumentFactory()
        doc_id = doc.pk
        actor = UserFactory()
        services.delete_document(document=doc, deleted_by=actor)
        assert not Document.objects.filter(pk=doc_id).exists()
        assert Document.all_objects.filter(pk=doc_id).exists()

    def test_delete_logs_audit_entry(self):
        doc = DocumentFactory()
        doc_id = doc.pk
        actor = UserFactory()
        services.delete_document(document=doc, deleted_by=actor)
        assert AuditLog.objects.filter(action=AuditLog.Action.DELETE, module="document", entity_id=str(doc_id)).exists()


class TestRecordDownload:
    def test_download_logs_audit_entry(self):
        doc = DocumentFactory()
        actor = UserFactory()
        services.record_download(document=doc, user=actor)
        assert AuditLog.objects.filter(action=AuditLog.Action.DOWNLOAD, module="document", entity_id=str(doc.pk)).exists()


class TestGetDocumentsFor:
    def test_returns_only_documents_for_that_object(self):
        vehicle_a = VehicleFactory()
        vehicle_b = VehicleFactory()
        doc_a = DocumentFactory(content_object=vehicle_a)
        DocumentFactory(content_object=vehicle_b)

        results = services.get_documents_for(vehicle_a)
        assert doc_a in results
        assert results.count() == 1

    def test_excludes_superseded_versions(self):
        vehicle = VehicleFactory()
        original = DocumentFactory(content_object=vehicle, version=1)
        newer = DocumentFactory(content_object=vehicle, replaces=original, version=2)

        results = services.get_documents_for(vehicle)
        assert newer in results
        assert original not in results
