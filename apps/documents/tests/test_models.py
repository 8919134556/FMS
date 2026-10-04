import datetime

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.documents.models import Document
from apps.documents.tests.factories import DocumentFactory, make_pdf
from apps.vehicles.tests.factories import VehicleFactory

pytestmark = pytest.mark.django_db


class TestDocumentUniqueness:
    def test_duplicate_document_number_rejected(self):
        DocumentFactory(document_number="DOC-999999")
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                DocumentFactory(document_number="DOC-999999")


class TestGenericRelation:
    def test_content_object_resolves_to_attached_entity(self):
        vehicle = VehicleFactory(registration_number="KA09DOC001")
        doc = DocumentFactory(content_object=vehicle)
        assert doc.content_object == vehicle
        assert doc.content_type == ContentType.objects.get_for_model(vehicle)
        assert doc.object_id == vehicle.pk


class TestExpiryLogic:
    def test_no_expiry_date_is_no_expiry(self):
        doc = DocumentFactory(expiry_date=None)
        assert doc.expiry_status == "NO_EXPIRY"
        assert doc.is_expired is False
        assert doc.is_expiring_soon is False
        assert doc.days_until_expiry is None

    def test_future_date_beyond_window_is_valid(self):
        doc = DocumentFactory(expiry_date=timezone.now().date() + datetime.timedelta(days=Document.EXPIRING_SOON_WINDOW_DAYS + 10))
        assert doc.expiry_status == "VALID"
        assert doc.is_expired is False
        assert doc.is_expiring_soon is False

    def test_date_within_window_is_expiring_soon(self):
        doc = DocumentFactory(expiry_date=timezone.now().date() + datetime.timedelta(days=10))
        assert doc.expiry_status == "EXPIRING_SOON"
        assert doc.is_expiring_soon is True
        assert doc.is_expired is False

    def test_past_date_is_expired(self):
        doc = DocumentFactory(expiry_date=timezone.now().date() - datetime.timedelta(days=5))
        assert doc.expiry_status == "EXPIRED"
        assert doc.is_expired is True
        assert doc.is_expiring_soon is False

    def test_expiry_label_for_expiring_soon(self):
        doc = DocumentFactory(expiry_date=timezone.now().date() + datetime.timedelta(days=12))
        assert doc.expiry_label == "12 days left"

    def test_expiry_label_for_expired(self):
        doc = DocumentFactory(expiry_date=timezone.now().date() - datetime.timedelta(days=23))
        assert doc.expiry_label == "Expired 23 days ago"

    def test_expiry_label_no_expiry(self):
        doc = DocumentFactory(expiry_date=None)
        assert doc.expiry_label == "Never"


class TestDocumentQuerySet:
    def test_expired_queryset(self):
        expired = DocumentFactory(expiry_date=timezone.now().date() - datetime.timedelta(days=1))
        DocumentFactory(expiry_date=timezone.now().date() + datetime.timedelta(days=60))
        results = Document.objects.expired()
        assert expired in results
        assert results.count() == 1

    def test_expiring_soon_queryset(self):
        expiring = DocumentFactory(expiry_date=timezone.now().date() + datetime.timedelta(days=5))
        DocumentFactory(expiry_date=timezone.now().date() + datetime.timedelta(days=60))
        results = Document.objects.expiring_soon()
        assert expiring in results
        assert results.count() == 1

    def test_no_expiry_queryset(self):
        no_expiry = DocumentFactory(expiry_date=None)
        DocumentFactory(expiry_date=timezone.now().date() + datetime.timedelta(days=5))
        results = Document.objects.no_expiry()
        assert no_expiry in results
        assert results.count() == 1

    def test_current_versions_excludes_superseded(self):
        original = DocumentFactory()
        newer = DocumentFactory(replaces=original, version=2)
        results = Document.objects.current_versions()
        assert newer in results
        assert original not in results


class TestVersioning:
    def test_is_current_version_true_for_standalone_document(self):
        doc = DocumentFactory()
        assert doc.is_current_version is True

    def test_is_current_version_false_once_superseded(self):
        original = DocumentFactory()
        DocumentFactory(replaces=original, version=2)
        original.refresh_from_db()
        assert original.is_current_version is False

    def test_new_version_is_current(self):
        original = DocumentFactory()
        newer = DocumentFactory(replaces=original, version=2)
        assert newer.is_current_version is True


class TestFileMetadata:
    def test_file_size_display_bytes(self):
        doc = DocumentFactory(file_size=500)
        assert doc.file_size_display == "500 B"

    def test_file_size_display_kb(self):
        doc = DocumentFactory(file_size=2048)
        assert "KB" in doc.file_size_display

    def test_is_previewable_pdf(self):
        doc = DocumentFactory(original_filename="report.pdf")
        assert doc.is_previewable is True

    def test_is_previewable_false_for_docx(self):
        doc = DocumentFactory(original_filename="report.docx")
        assert doc.is_previewable is False
