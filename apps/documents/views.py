"""Document CRUD, list/filter/export, protected download, and the
archive/restore/delete/replace workflow.

Mirrors the rest of the FMS (same permission mixins, same audit logging
shape, same services-layer pattern). The one deliberate difference from
Vehicle/Driver/etc: Document uses plain Forms, not ModelForms, because the
generic (content_type, object_id) relationship isn't a normal form field.
"""

import csv
import mimetypes
import os

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST
from django.views.generic import DetailView, FormView, ListView

from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.core.permissions import ModulePermissionRequiredMixin, module_permission_required, user_has_permission
from apps.documents import services
from apps.documents.forms import DocumentEditForm, DocumentForm, DocumentReplaceForm
from apps.documents.models import Document
from apps.documents.registry import DOCUMENTABLE_MODELS, ENTITY_TYPE_CHOICES, get_entity_config

DOCUMENT_SELECT_RELATED = ("content_type",)


class DocumentListView(ModulePermissionRequiredMixin, ListView):
    model = Document
    template_name = "documents/document_list.html"
    context_object_name = "documents"
    paginate_by = 25
    permission_module = "document"
    permission_action = "view"

    def get_queryset(self):
        qs = services.scope_documents(
            Document.objects.select_related(*DOCUMENT_SELECT_RELATED).prefetch_related("content_object"), self.request.user
        )

        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(
                Q(document_number__icontains=query)
                | Q(title__icontains=query)
                | Q(original_filename__icontains=query)
            )

        document_type = self.request.GET.get("document_type", "").strip()
        if document_type:
            qs = qs.filter(document_type=document_type)

        entity_type = self.request.GET.get("entity_type", "").strip()
        if entity_type:
            config = get_entity_config(entity_type)
            if config:
                from django.contrib.contenttypes.models import ContentType

                qs = qs.filter(content_type=ContentType.objects.get_for_model(config["model"]))

        status = self.request.GET.get("status", "").strip()
        if status:
            qs = qs.filter(status=status)

        expiry = self.request.GET.get("expiry", "").strip()
        if expiry == "expired":
            qs = qs.expired()
        elif expiry == "expiring_soon":
            qs = qs.expiring_soon()
        elif expiry == "valid":
            qs = qs.valid()
        elif expiry == "no_expiry":
            qs = qs.no_expiry()

        uploaded_by = self.request.GET.get("uploaded_by", "").strip()
        if uploaded_by:
            qs = qs.filter(created_by_id=uploaded_by)

        date_from = self.request.GET.get("date_from", "").strip()
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)

        date_to = self.request.GET.get("date_to", "").strip()
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)

        return qs.order_by("-created_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        filters = {
            name: self.request.GET.get(name, "")
            for name in ["q", "document_type", "entity_type", "status", "expiry", "uploaded_by", "date_from", "date_to"]
        }
        context["current_filters"] = filters
        context["filter_fields"] = [
            {"type": "search", "name": "q", "label": "Search", "value": filters["q"], "col": 3,
             "placeholder": "Document number, title, filename…"},
            {"type": "select", "name": "entity_type", "label": "Entity Type", "value": filters["entity_type"], "col": 2,
             "choices": ENTITY_TYPE_CHOICES},
            {"type": "select", "name": "document_type", "label": "Type", "value": filters["document_type"], "col": 2,
             "choices": Document.DocumentType.choices},
            {"type": "select", "name": "status", "label": "Status", "value": filters["status"], "col": 2,
             "choices": Document.Status.choices},
            {"type": "select", "name": "expiry", "label": "Expiry", "value": filters["expiry"], "col": 2,
             "choices": [("valid", "Valid"), ("expiring_soon", "Expiring Soon"), ("expired", "Expired"), ("no_expiry", "No Expiry")]},
            {"type": "date", "name": "date_from", "label": "From", "value": filters["date_from"], "col": 1},
            {"type": "date", "name": "date_to", "label": "To", "value": filters["date_to"], "col": 1},
        ]
        querystring = self.request.GET.urlencode()
        context["export_url"] = reverse("documents:document_export") + (f"?{querystring}" if querystring else "")
        context["create_url"] = reverse("documents:document_create")
        return context


class DocumentExportView(DocumentListView):
    permission_module = "document"
    permission_action = "export"
    paginate_by = None

    def get(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="documents_export.csv"'
        writer = csv.writer(response)
        writer.writerow(
            ["Document Number", "Title", "Type", "Related Entity", "Issue Date", "Expiry Date",
             "Status", "Uploaded By", "Uploaded At"]
        )
        for d in queryset:
            writer.writerow(
                [
                    d.document_number, d.title, d.get_document_type_display(),
                    str(d.content_object) if d.content_object else "",
                    d.issue_date or "", d.expiry_date or "", d.get_status_display(),
                    d.created_by.get_full_name() if d.created_by_id else "", d.created_at,
                ]
            )
        log_action(
            action=AuditLog.Action.EXPORT, module="document", entity="Document", entity_id="",
            new_value={"row_count": queryset.count()}, user=request.user, request=request,
        )
        return response


class DocumentCreateView(ModulePermissionRequiredMixin, FormView):
    form_class = DocumentForm
    template_name = "documents/document_form.html"
    permission_module = "document"
    permission_action = "create"

    def get_locked_entity(self):
        """Entity-scoped upload (e.g. "Add Document" from a Vehicle's own
        page) arrives with ?entity_type=vehicle&object_id=<pk> and the form
        renders that relationship as fixed, not re-selectable — see the
        "Do not force the administrator to select the same vehicle again"
        requirement."""
        entity_type = self.request.GET.get("entity_type", "").strip()
        object_id = self.request.GET.get("object_id", "").strip()
        if not (entity_type and object_id):
            return None
        config = get_entity_config(entity_type)
        if not config:
            return None
        # Resolving the record confirms it exists and reveals its label, so it
        # needs the same view access to that entity's own module.
        if not user_has_permission(self.request.user, entity_type, "view"):
            return None
        obj = config["model"].objects.filter(pk=object_id).first()
        if not obj:
            return None
        return {"entity_type": entity_type, "object_id": object_id, "object": obj, "label": config["label"]}

    def get_initial(self):
        initial = super().get_initial()
        locked = self.get_locked_entity()
        if locked:
            initial["entity_type"] = locked["entity_type"]
            initial["object_id"] = locked["object_id"]
        return initial

    def get_context_data(self, **kwargs):
        from django.conf import settings

        context = super().get_context_data(**kwargs)
        context["locked_entity"] = self.get_locked_entity()
        context["max_upload_mb"] = getattr(settings, "MAX_UPLOAD_SIZE_MB", 5)
        return context

    def form_valid(self, form):
        content_object = form.get_content_object()
        try:
            document = services.upload_document(
                content_object=content_object,
                title=form.cleaned_data["title"],
                document_type=form.cleaned_data["document_type"],
                description=form.cleaned_data["description"],
                file=form.cleaned_data["file"],
                issue_date=form.cleaned_data["issue_date"],
                expiry_date=form.cleaned_data["expiry_date"],
                notes=form.cleaned_data["notes"],
                uploaded_by=self.request.user,
                request=self.request,
            )
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc))
            return self.form_invalid(form)
        self.document = document
        messages.success(self.request, f"Document {document.document_number} uploaded successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("documents:document_detail", kwargs={"uuid": self.document.uuid})


class DocumentUpdateView(ModulePermissionRequiredMixin, FormView):
    form_class = DocumentEditForm
    template_name = "documents/document_edit_form.html"
    permission_module = "document"
    permission_action = "update"

    def dispatch(self, request, *args, **kwargs):
        self.document = get_object_or_404(Document, uuid=kwargs["uuid"])
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):
        return {
            "document_type": self.document.document_type,
            "title": self.document.title,
            "description": self.document.description,
            "issue_date": self.document.issue_date,
            "expiry_date": self.document.expiry_date,
            "notes": self.document.notes,
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["document"] = self.document
        return context

    def form_valid(self, form):
        old_value = {
            "title": self.document.title, "document_type": self.document.document_type,
            "expiry_date": str(self.document.expiry_date) if self.document.expiry_date else None,
        }
        for field in ["document_type", "title", "description", "issue_date", "expiry_date", "notes"]:
            setattr(self.document, field, form.cleaned_data[field])
        self.document.updated_by = self.request.user
        self.document.save()

        log_action(
            action=AuditLog.Action.UPDATE, module="document", entity="Document", entity_id=str(self.document.pk),
            old_value=old_value,
            new_value={"title": self.document.title, "document_type": self.document.document_type},
            user=self.request.user, request=self.request,
        )
        messages.success(self.request, f"Document {self.document.document_number} was updated successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("documents:document_detail", kwargs={"uuid": self.document.uuid})


class DocumentDetailView(ModulePermissionRequiredMixin, DetailView):
    model = Document
    template_name = "documents/document_detail.html"
    context_object_name = "document"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "document"
    permission_action = "view"

    def get_queryset(self):
        return services.scope_documents(Document.objects.all(), self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        document = self.object
        config = get_entity_config(_entity_type_for_document(document))
        context["entity_label"] = config["label"] if config else document.content_type.name
        context["entity_url"] = _entity_detail_url(document)
        context["replace_form"] = DocumentReplaceForm()
        context["version_history"] = _version_chain(document)
        context["recent_activity"] = AuditLog.objects.filter(
            module="document", entity="Document", entity_id=str(document.pk)
        ).select_related("user").order_by("-timestamp")[:30]
        return context


def _entity_type_for_document(document):
    model = document.content_type.model_class()
    for key, config in DOCUMENTABLE_MODELS.items():
        if config["model"] is model:
            return key
    return None


def _entity_detail_url(document):
    entity_type = _entity_type_for_document(document)
    config = get_entity_config(entity_type)
    if not config or not document.content_object:
        return None
    try:
        return reverse(config["detail_url"], kwargs={"uuid": document.content_object.uuid})
    except Exception:
        return None


def _version_chain(document):
    """Oldest -> newest, so the template can render a simple timeline."""
    chain = [document]
    current = document
    while current.replaces_id:
        current = current.replaces
        chain.append(current)
    chain.reverse()
    while True:
        try:
            newer = chain[-1].superseded_by
        except Document.DoesNotExist:
            break
        if newer is None:
            break
        chain.append(newer)
    return chain


@require_GET
@module_permission_required("document", "view")
def document_download(request, uuid):
    """The ONLY sanctioned way to fetch a document's file — templates never
    render document.file.url directly, so there is no bare /media/... link
    to guess. Still gated by the same module-permission check as every
    other protected view in this app."""
    document = get_object_or_404(services.scope_documents(Document.objects.all(), request.user), uuid=uuid)
    if not document.file:
        raise Http404("File not found.")

    services.record_download(document=document, user=request.user, request=request)

    content_type = mimetypes.guess_type(document.original_filename)[0] or "application/octet-stream"
    response = FileResponse(document.file.open("rb"), content_type=content_type)
    safe_name = os.path.basename(document.original_filename) or f"{document.document_number}.bin"
    response["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    return response


@require_GET
@module_permission_required("document", "view")
def document_preview(request, uuid):
    """Inline (non-attachment) rendering for PDF/image previews only —
    everything else falls back to Download, no custom viewer is built."""
    document = get_object_or_404(services.scope_documents(Document.objects.all(), request.user), uuid=uuid)
    if not document.file:
        raise Http404("File not found.")
    ext = os.path.splitext(document.original_filename)[1].lower()
    if ext not in {".pdf", ".jpg", ".jpeg", ".png"}:
        raise Http404("Preview not available for this file type.")

    content_type = mimetypes.guess_type(document.original_filename)[0] or "application/octet-stream"
    response = FileResponse(document.file.open("rb"), content_type=content_type)
    response["Content-Disposition"] = "inline"
    return response


@require_POST
@module_permission_required("document", "archive")
def document_archive(request, uuid):
    document = get_object_or_404(Document, uuid=uuid)
    try:
        services.archive_document(document=document, archived_by=request.user, request=request)
        messages.success(request, f"Document {document.document_number} was archived.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return redirect("documents:document_detail", uuid=document.uuid)


@require_POST
@module_permission_required("document", "archive")
def document_restore(request, uuid):
    document = get_object_or_404(Document, uuid=uuid)
    try:
        services.restore_document(document=document, restored_by=request.user, request=request)
        messages.success(request, f"Document {document.document_number} was restored.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return redirect("documents:document_detail", uuid=document.uuid)


@require_POST
@module_permission_required("document", "delete")
def document_delete(request, uuid):
    document = get_object_or_404(Document, uuid=uuid)
    document_number = document.document_number
    services.delete_document(document=document, deleted_by=request.user, request=request)
    messages.success(request, f"Document {document_number} was permanently deleted.")
    return redirect("documents:document_list")


@require_POST
@module_permission_required("document", "update")
def document_replace(request, uuid):
    document = get_object_or_404(Document, uuid=uuid)
    form = DocumentReplaceForm(request.POST, request.FILES)
    if form.is_valid():
        try:
            new_document = services.replace_document(
                document=document, file=form.cleaned_data["file"], replaced_by=request.user, request=request,
            )
            messages.success(request, f"Uploaded new version {new_document.document_number} (v{new_document.version}).")
            return redirect("documents:document_detail", uuid=new_document.uuid)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    else:
        error_text = "; ".join(f"{field}: {', '.join(errs)}" for field, errs in form.errors.items())
        messages.error(request, f"Could not upload new version — {error_text}")
    return redirect("documents:document_detail", uuid=document.uuid)


@require_GET
@module_permission_required("document", "view")
def document_related_options(request):
    """HTMX partial: <option> list of records for the chosen entity type,
    optionally filtered by a search term — avoids ever rendering every
    Client/Vehicle/Driver/... row into the create form up front."""
    entity_type = request.GET.get("entity_type", "").strip()
    query = request.GET.get("q", "").strip()
    config = get_entity_config(entity_type)
    # This endpoint lists/searches records of *another* module (clients,
    # vehicles, drivers, ...), so document.view alone is not enough — the
    # viewer must be able to open that module too. Otherwise: no options.
    if not config or not user_has_permission(request.user, entity_type, "view"):
        return render(request, "documents/_related_options.html", {"objects": []})

    model = config["model"]
    qs = model.objects.all()
    if query:
        search_fields = _SEARCH_FIELDS.get(entity_type, [])
        if search_fields:
            condition = Q()
            for field in search_fields:
                condition |= Q(**{f"{field}__icontains": query})
            qs = qs.filter(condition)
    objects = list(qs.order_by("pk")[:50])
    return render(request, "documents/_related_options.html", {"objects": objects})


_SEARCH_FIELDS = {
    "client": ["client_name", "client_code"],
    "site": ["site_name", "site_code"],
    "vehicle": ["registration_number", "make", "model"],
    "driver": ["first_name", "last_name", "employee_id"],
    "trip": ["trip_number"],
    "maintenance": ["maintenance_number"],
}
