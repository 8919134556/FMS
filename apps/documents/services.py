"""Document lifecycle business logic: number generation, upload, versioning,
archive/restore/delete, and the audit trail for all of it.

Kept out of the views/forms for the same reason every other module's
services.py is — one obvious, testable entry point per operation.
"""

import mimetypes

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.core.scoping import client_scope_id
from apps.documents.models import Document, DocumentNumberSequence
from apps.documents.registry import DOCUMENTABLE_MODELS

DOCUMENT_AUDIT_FIELDS = ["document_number", "title", "document_type", "status", "expiry_date"]


# How each documentable entity reaches its client — the path scope_documents
# follows to decide whether a document belongs to the viewer's client.
_ENTITY_CLIENT_LOOKUP = {
    "client": "id", "site": "client_id", "vehicle": "client_id",
    "driver": "client_id", "trip": "client_id", "maintenance": "vehicle__client_id",
}


def scope_documents(qs, user):
    """Documents attach to any entity through a generic FK, so client scoping
    can't be a single column filter: a document is visible to a client user
    only if the record it is attached to belongs to that client. No-op for
    staff. Entity types missing from ``_ENTITY_CLIENT_LOOKUP`` are excluded
    (fail closed) so a newly documentable model can't leak by omission."""
    client_id = client_scope_id(user)
    if client_id is None:
        return qs
    condition = Q(pk__in=[])
    for key, config in DOCUMENTABLE_MODELS.items():
        lookup = _ENTITY_CLIENT_LOOKUP.get(key)
        if lookup is None:
            continue
        model = config["model"]
        owned = model.objects.filter(**{lookup: client_id}).values("pk")
        condition |= Q(content_type=ContentType.objects.get_for_model(model), object_id__in=owned)
    return qs.filter(condition)


def generate_document_number():
    with transaction.atomic():
        seq, _ = DocumentNumberSequence.objects.select_for_update().get_or_create(id=1)
        seq.last_value += 1
        seq.save(update_fields=["last_value"])
        return f"DOC-{seq.last_value:06d}"


@transaction.atomic
def upload_document(
    *, content_object, title, document_type, description, file, issue_date, expiry_date, notes, uploaded_by, request=None,
):
    document = Document(
        document_number=generate_document_number(),
        title=title,
        document_type=document_type,
        description=description,
        file=file,
        original_filename=file.name,
        file_size=file.size,
        mime_type=mimetypes.guess_type(file.name)[0] or "",
        issue_date=issue_date,
        expiry_date=expiry_date,
        notes=notes,
        content_object=content_object,
        created_by=uploaded_by,
        updated_by=uploaded_by,
    )
    document.full_clean()
    document.save()

    log_action(
        action=AuditLog.Action.CREATE, module="document", entity="Document", entity_id=str(document.pk),
        new_value={"document_number": document.document_number, "title": title, "document_type": document_type},
        user=uploaded_by, request=request,
    )
    return document


@transaction.atomic
def replace_document(*, document, file, replaced_by, request=None):
    """Uploads a new version and links it to the one it supersedes. The old
    row is untouched (still readable in history via ``.replaces``); the new
    row becomes the current version — see Document.is_current_version."""
    if not document.is_current_version:
        raise ValidationError("Only the current version of a document can be replaced.")

    new_document = Document(
        document_number=generate_document_number(),
        title=document.title,
        document_type=document.document_type,
        description=document.description,
        file=file,
        original_filename=file.name,
        file_size=file.size,
        mime_type=mimetypes.guess_type(file.name)[0] or "",
        issue_date=document.issue_date,
        expiry_date=document.expiry_date,
        notes=document.notes,
        version=document.version + 1,
        replaces=document,
        content_object=document.content_object,
        created_by=replaced_by,
        updated_by=replaced_by,
    )
    new_document.full_clean()
    new_document.save()

    log_action(
        action=AuditLog.Action.CREATE, module="document", entity="Document", entity_id=str(new_document.pk),
        new_value={"document_number": new_document.document_number, "replaces": document.pk, "version": new_document.version},
        user=replaced_by, request=request,
    )
    return new_document


@transaction.atomic
def archive_document(*, document, archived_by, request=None):
    if document.status == Document.Status.ARCHIVED:
        raise ValidationError("This document is already archived.")
    document.status = Document.Status.ARCHIVED
    document.updated_by = archived_by
    document.save(update_fields=["status", "updated_by", "updated_at"])

    log_action(
        action=AuditLog.Action.ARCHIVE, module="document", entity="Document", entity_id=str(document.pk),
        new_value={"status": document.status}, user=archived_by, request=request,
    )
    return document


@transaction.atomic
def restore_document(*, document, restored_by, request=None):
    if document.status == Document.Status.ACTIVE:
        raise ValidationError("This document is not archived.")
    document.status = Document.Status.ACTIVE
    document.updated_by = restored_by
    document.save(update_fields=["status", "updated_by", "updated_at"])

    log_action(
        action=AuditLog.Action.RESTORE, module="document", entity="Document", entity_id=str(document.pk),
        new_value={"status": document.status}, user=restored_by, request=request,
    )
    return document


@transaction.atomic
def delete_document(*, document, deleted_by, request=None):
    """Soft delete (apps.core.models.SoftDeleteModel) — the row survives at
    the DB level for recovery via Document.all_objects, but disappears from
    every normal query (Document.objects), matching "permanent deletion
    should be restricted" without an unrecoverable hard DELETE."""
    document_id = document.pk
    document_number = document.document_number
    document.delete(deleted_by=deleted_by)

    log_action(
        action=AuditLog.Action.DELETE, module="document", entity="Document", entity_id=str(document_id),
        old_value={"document_number": document_number}, user=deleted_by, request=request,
    )


def record_download(*, document, user, request=None):
    log_action(
        action=AuditLog.Action.DOWNLOAD, module="document", entity="Document", entity_id=str(document.pk),
        new_value={"document_number": document.document_number}, user=user, request=request,
    )


def get_documents_for(obj):
    """Current-version, active-or-archived documents attached to ``obj`` —
    the one query every entity detail page's Documents tab calls, instead
    of six separate ad-hoc ContentType lookups."""
    from django.contrib.contenttypes.models import ContentType

    content_type = ContentType.objects.get_for_model(obj)
    return (
        Document.objects.filter(content_type=content_type, object_id=obj.pk)
        .current_versions()
        .order_by("-created_at")
    )
