from django.contrib import admin

from apps.documents.models import Document, DocumentNumberSequence


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("document_number", "title", "document_type", "content_type", "object_id", "status", "expiry_date")
    list_filter = ("status", "document_type", "content_type")
    search_fields = ("document_number", "title", "original_filename")

    def get_queryset(self, request):
        return Document.all_objects.all()


@admin.register(DocumentNumberSequence)
class DocumentNumberSequenceAdmin(admin.ModelAdmin):
    list_display = ("id", "last_value")
