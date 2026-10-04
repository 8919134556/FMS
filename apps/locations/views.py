"""Client Site CRUD, list/filter/export.

Mirrors apps.clients.views (same permission mixins, same audit logging
shape) so Sites behaves like the same product rather than a
separately-invented module. Branch has no dedicated CRUD UI yet (managed
via django-admin, same as Vehicle Types) — only Site gets one this pass.
"""

import csv

from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.clients.models import Client
from apps.core.permissions import ModulePermissionRequiredMixin, module_permission_required, restrict_detail_tabs
from apps.core.scoping import client_scoped, is_client_scoped, scope_queryset
from apps.core.utils import model_to_dict_safe
from apps.documents.services import get_documents_for
from apps.locations.forms import BranchForm, SiteForm
from apps.locations.models import Branch, Site

SITE_AUDIT_FIELDS = ["site_code", "site_name", "site_type", "status", "client_id", "branch_id"]
BRANCH_AUDIT_FIELDS = ["code", "name", "branch_type", "status", "client_id", "manager_id"]


@client_scoped("client_id")
class SiteListView(ModulePermissionRequiredMixin, ListView):
    model = Site
    template_name = "locations/site_list.html"
    context_object_name = "sites"
    paginate_by = 25
    permission_module = "site"
    permission_action = "view"

    def get_queryset(self):
        qs = Site.objects.select_related("client", "branch").all()

        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(
                Q(site_name__icontains=query)
                | Q(site_code__icontains=query)
                | Q(city__icontains=query)
                | Q(contact_name__icontains=query)
                | Q(contact_phone__icontains=query)
            )

        status = self.request.GET.get("status", "").strip()
        if status:
            qs = qs.filter(status=status)

        site_type = self.request.GET.get("site_type", "").strip()
        if site_type:
            qs = qs.filter(site_type=site_type)

        client = self.request.GET.get("client", "").strip()
        if client:
            qs = qs.filter(client_id=client)

        branch = self.request.GET.get("branch", "").strip()
        if branch:
            qs = qs.filter(branch_id=branch)

        return qs.order_by("site_name")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        q = self.request.GET.get("q", "")
        status = self.request.GET.get("status", "")
        site_type = self.request.GET.get("site_type", "")
        client = self.request.GET.get("client", "")
        branch = self.request.GET.get("branch", "")
        context["current_filters"] = {
            "q": q, "status": status, "site_type": site_type, "client": client, "branch": branch,
        }
        context["filter_fields"] = [
            {"type": "search", "name": "q", "label": "Search", "value": q, "col": 3,
             "placeholder": "Site name, code, city, contact…"},
            {"type": "select", "name": "status", "label": "Status", "value": status, "col": 2,
             "choices": Site.Status.choices},
            {"type": "select", "name": "site_type", "label": "Type", "value": site_type, "col": 2,
             "choices": Site.SiteType.choices},
            {"type": "select", "name": "client", "label": "Client", "value": client, "col": 2,
             "choices": [(str(c.id), c.client_name) for c in scope_queryset(Client.objects.filter(status=Client.Status.ACTIVE), self.request.user, "id")]},
            {"type": "select", "name": "branch", "label": "Branch", "value": branch, "col": 2,
             "choices": [(str(b.id), b.name) for b in Branch.objects.filter(status=Branch.Status.ACTIVE) if not is_client_scoped(self.request.user)]},
        ]
        querystring = self.request.GET.urlencode()
        context["export_url"] = reverse("locations:site_export") + (f"?{querystring}" if querystring else "")
        context["create_url"] = reverse("locations:site_create")
        return context


class SiteExportView(SiteListView):
    permission_module = "site"
    permission_action = "export"
    paginate_by = None

    def get(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="sites_export.csv"'
        writer = csv.writer(response)
        writer.writerow(
            ["Site Code", "Site Name", "Type", "Client", "Branch", "City", "Contact", "Phone", "Status"]
        )
        for s in queryset:
            writer.writerow(
                [
                    s.site_code, s.site_name, s.get_site_type_display(), s.client.client_name,
                    s.branch.name if s.branch_id else "", s.city, s.contact_name, s.contact_phone,
                    s.get_status_display(),
                ]
            )
        log_action(
            action=AuditLog.Action.EXPORT, module="site", entity="Site", entity_id="",
            new_value={"row_count": queryset.count()}, user=request.user, request=request,
        )
        return response


class SiteCreateView(ModulePermissionRequiredMixin, CreateView):
    model = Site
    form_class = SiteForm
    template_name = "locations/site_form.html"
    permission_module = "site"
    permission_action = "create"

    def form_valid(self, form):
        self.object = form.save(actor=self.request.user)
        log_action(
            action=AuditLog.Action.CREATE, module="site", entity="Site", entity_id=str(self.object.pk),
            new_value=model_to_dict_safe(self.object, SITE_AUDIT_FIELDS),
            user=self.request.user, request=self.request,
        )
        messages.success(self.request, f"Site '{self.object.site_name}' was created successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("locations:site_detail", kwargs={"uuid": self.object.uuid})


class SiteUpdateView(ModulePermissionRequiredMixin, UpdateView):
    model = Site
    form_class = SiteForm
    template_name = "locations/site_form.html"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "site"
    permission_action = "update"

    def form_valid(self, form):
        old_value = model_to_dict_safe(self.get_object(), SITE_AUDIT_FIELDS)
        self.object = form.save(actor=self.request.user)
        log_action(
            action=AuditLog.Action.UPDATE, module="site", entity="Site", entity_id=str(self.object.pk),
            old_value=old_value, new_value=model_to_dict_safe(self.object, SITE_AUDIT_FIELDS),
            user=self.request.user, request=self.request,
        )
        messages.success(self.request, f"Site '{self.object.site_name}' was updated successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("locations:site_detail", kwargs={"uuid": self.object.uuid})


SITE_TAB_MODULES = {
    "trips": "trip",
    "documents": "document",
    "history": "audit_log",
}


@client_scoped("client_id")
class SiteDetailView(ModulePermissionRequiredMixin, DetailView):
    model = Site
    template_name = "locations/site_detail.html"
    context_object_name = "site"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "site"
    permission_action = "view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        active_tab = self.request.GET.get("tab", "overview")
        context["active_tab"] = active_tab
        origin_trips = scope_queryset(self.object.trips_as_origin.select_related("client", "vehicle", "driver", "destination_site"), self.request.user)
        destination_trips = scope_queryset(self.object.trips_as_destination.select_related("client", "vehicle", "driver", "origin_site"), self.request.user)
        context["origin_trips"] = origin_trips.order_by("-scheduled_start")[:25]
        context["destination_trips"] = destination_trips.order_by("-scheduled_start")[:25]
        context["site_trip_count"] = origin_trips.count() + destination_trips.count()
        context["recent_activity"] = AuditLog.objects.filter(
            module="site", entity="Site", entity_id=str(self.object.pk)
        ).select_related("user").order_by("-timestamp")[:20]
        context["documents"] = get_documents_for(self.object)

        base_url = reverse("locations:site_detail", kwargs={"uuid": self.object.uuid})
        context["tab_list"] = [
            {"id": "overview", "label": "Overview", "icon": "info-circle", "url": base_url},
            {"id": "trips", "label": "Trips", "icon": "map", "url": f"{base_url}?tab=trips"},
            {"id": "documents", "label": "Documents", "icon": "file-earmark-check", "url": f"{base_url}?tab=documents"},
            {"id": "history", "label": "History", "icon": "clock-history", "url": f"{base_url}?tab=history"},
        ]
        restrict_detail_tabs(self.request.user, context, SITE_TAB_MODULES)
        return context


def _set_site_status(request, uuid, new_status, note):
    site = get_object_or_404(Site, uuid=uuid)
    old_status = site.status
    site.status = new_status
    site.updated_by = request.user
    site.save(update_fields=["status", "updated_by", "updated_at"])
    log_action(
        action=AuditLog.Action.UPDATE, module="site", entity="Site", entity_id=str(site.pk),
        old_value={"status": old_status}, new_value={"status": new_status},
        user=request.user, request=request,
    )
    messages.success(request, f"{site.site_name} {note}.")
    return redirect("locations:site_detail", uuid=site.uuid)


@require_POST
@module_permission_required("site", "archive")
def site_deactivate(request, uuid):
    return _set_site_status(request, uuid, Site.Status.INACTIVE, "has been deactivated")


@require_POST
@module_permission_required("site", "archive")
def site_activate(request, uuid):
    return _set_site_status(request, uuid, Site.Status.ACTIVE, "has been reactivated")


class BranchListView(ModulePermissionRequiredMixin, ListView):
    model = Branch
    template_name = "locations/branch_list.html"
    context_object_name = "branches"
    paginate_by = 25
    permission_module = "branch"
    permission_action = "view"

    def get_queryset(self):
        qs = Branch.objects.select_related("client", "manager").all()

        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(
                Q(name__icontains=query) | Q(code__icontains=query) | Q(city__icontains=query)
            )

        status = self.request.GET.get("status", "").strip()
        if status:
            qs = qs.filter(status=status)

        branch_type = self.request.GET.get("branch_type", "").strip()
        if branch_type:
            qs = qs.filter(branch_type=branch_type)

        client = self.request.GET.get("client", "").strip()
        if client:
            qs = qs.filter(client_id=client)

        return qs.order_by("name")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        q = self.request.GET.get("q", "")
        status = self.request.GET.get("status", "")
        branch_type = self.request.GET.get("branch_type", "")
        client = self.request.GET.get("client", "")
        context["current_filters"] = {"q": q, "status": status, "branch_type": branch_type, "client": client}
        context["filter_fields"] = [
            {"type": "search", "name": "q", "label": "Search", "value": q, "col": 3,
             "placeholder": "Branch name, code, city…"},
            {"type": "select", "name": "status", "label": "Status", "value": status, "col": 2,
             "choices": Branch.Status.choices},
            {"type": "select", "name": "branch_type", "label": "Type", "value": branch_type, "col": 2,
             "choices": Branch.BranchType.choices},
            {"type": "select", "name": "client", "label": "Client", "value": client, "col": 3,
             "choices": [(str(c.id), c.client_name) for c in Client.objects.filter(status=Client.Status.ACTIVE)]},
        ]
        querystring = self.request.GET.urlencode()
        context["export_url"] = reverse("locations:branch_export") + (f"?{querystring}" if querystring else "")
        context["create_url"] = reverse("locations:branch_create")
        return context


class BranchExportView(BranchListView):
    permission_module = "branch"
    permission_action = "export"
    paginate_by = None

    def get(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="branches_export.csv"'
        writer = csv.writer(response)
        writer.writerow(["Code", "Name", "Type", "Client", "City", "Manager", "Phone", "Status"])
        for b in queryset:
            writer.writerow(
                [
                    b.code, b.name, b.get_branch_type_display(), b.client.client_name if b.client_id else "",
                    b.city, b.manager.get_full_name() if b.manager_id else "", b.phone, b.get_status_display(),
                ]
            )
        log_action(
            action=AuditLog.Action.EXPORT, module="branch", entity="Branch", entity_id="",
            new_value={"row_count": queryset.count()}, user=request.user, request=request,
        )
        return response


class BranchCreateView(ModulePermissionRequiredMixin, CreateView):
    model = Branch
    form_class = BranchForm
    template_name = "locations/branch_form.html"
    permission_module = "branch"
    permission_action = "create"

    def form_valid(self, form):
        self.object = form.save(actor=self.request.user)
        log_action(
            action=AuditLog.Action.CREATE, module="branch", entity="Branch", entity_id=str(self.object.pk),
            new_value=model_to_dict_safe(self.object, BRANCH_AUDIT_FIELDS),
            user=self.request.user, request=self.request,
        )
        messages.success(self.request, f"Branch '{self.object.name}' was created successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("locations:branch_detail", kwargs={"uuid": self.object.uuid})


class BranchUpdateView(ModulePermissionRequiredMixin, UpdateView):
    model = Branch
    form_class = BranchForm
    template_name = "locations/branch_form.html"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "branch"
    permission_action = "update"

    def form_valid(self, form):
        old_value = model_to_dict_safe(self.get_object(), BRANCH_AUDIT_FIELDS)
        self.object = form.save(actor=self.request.user)
        log_action(
            action=AuditLog.Action.UPDATE, module="branch", entity="Branch", entity_id=str(self.object.pk),
            old_value=old_value, new_value=model_to_dict_safe(self.object, BRANCH_AUDIT_FIELDS),
            user=self.request.user, request=self.request,
        )
        messages.success(self.request, f"Branch '{self.object.name}' was updated successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("locations:branch_detail", kwargs={"uuid": self.object.uuid})


BRANCH_TAB_MODULES = {
    "sites": "site",
    "vehicles": "vehicle",
    "history": "audit_log",
}


class BranchDetailView(ModulePermissionRequiredMixin, DetailView):
    model = Branch
    template_name = "locations/branch_detail.html"
    context_object_name = "branch"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "branch"
    permission_action = "view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        active_tab = self.request.GET.get("tab", "overview")
        context["active_tab"] = active_tab
        context["branch_sites"] = self.object.sites.select_related("client").order_by("site_name")[:25]
        context["branch_vehicles"] = self.object.vehicles.select_related("vehicle_type").order_by("registration_number")[:25]
        context["recent_activity"] = AuditLog.objects.filter(
            module="branch", entity="Branch", entity_id=str(self.object.pk)
        ).select_related("user").order_by("-timestamp")[:20]

        base_url = reverse("locations:branch_detail", kwargs={"uuid": self.object.uuid})
        context["tab_list"] = [
            {"id": "overview", "label": "Overview", "icon": "info-circle", "url": base_url},
            {"id": "sites", "label": "Sites", "icon": "geo-alt", "url": f"{base_url}?tab=sites"},
            {"id": "vehicles", "label": "Vehicles", "icon": "truck-front", "url": f"{base_url}?tab=vehicles"},
            {"id": "history", "label": "History", "icon": "clock-history", "url": f"{base_url}?tab=history"},
        ]
        restrict_detail_tabs(self.request.user, context, BRANCH_TAB_MODULES)
        return context


def _set_branch_status(request, uuid, new_status, note):
    branch = get_object_or_404(Branch, uuid=uuid)
    old_status = branch.status
    branch.status = new_status
    branch.updated_by = request.user
    branch.save(update_fields=["status", "updated_by", "updated_at"])
    log_action(
        action=AuditLog.Action.UPDATE, module="branch", entity="Branch", entity_id=str(branch.pk),
        old_value={"status": old_status}, new_value={"status": new_status},
        user=request.user, request=request,
    )
    messages.success(request, f"{branch.name} {note}.")
    return redirect("locations:branch_detail", uuid=branch.uuid)


@require_POST
@module_permission_required("branch", "archive")
def branch_deactivate(request, uuid):
    return _set_branch_status(request, uuid, Branch.Status.INACTIVE, "has been deactivated")


@require_POST
@module_permission_required("branch", "archive")
def branch_activate(request, uuid):
    return _set_branch_status(request, uuid, Branch.Status.ACTIVE, "has been reactivated")
