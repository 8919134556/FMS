import factory
from django.core.files.uploadedfile import SimpleUploadedFile
from factory.django import DjangoModelFactory

from apps.documents.models import Document
from apps.vehicles.tests.factories import VehicleFactory

PDF_BYTES = b"%PDF-1.4\n%fake pdf content for tests\n"


def make_pdf(name="test.pdf", content=PDF_BYTES):
    return SimpleUploadedFile(name, content, content_type="application/pdf")


class DocumentFactory(DjangoModelFactory):
    class Meta:
        model = Document

    document_number = factory.Sequence(lambda n: f"DOC-{n:06d}")
    title = factory.Sequence(lambda n: f"Document {n}")
    document_type = Document.DocumentType.OTHER
    status = Document.Status.ACTIVE
    original_filename = "test.pdf"
    file_size = len(PDF_BYTES)
    mime_type = "application/pdf"

    @factory.lazy_attribute
    def file(self):
        return make_pdf()

    @factory.lazy_attribute
    def content_object(self):
        return VehicleFactory()
