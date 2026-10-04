"""Client CRUD, list/filter/export.

Mirrors apps.vehicles.views / apps.drivers.views (same permission mixins,
same audit logging shape) so Clients behaves like the same product rather
than a separately-invented module. Sites, Contacts, and Billing tabs arrive
with the dedicated Sites module (Phase 2, Module 4).
"""

import csv

from django.contrib import messages
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.clients.forms import ClientForm
from apps.clients.models import Client
from apps.core.permissions import ModulePermissionRequiredMixin, module_permission_required, restrict_detail_tabs
from apps.core.scoping import client_scoped, is_client_scoped
from apps.core.utils import model_to_dict_safe
from apps.documents.services import get_documents_for

CLIENT_AUDIT_FIELDS = [
    "client_code", "client_name", "client_type", "status", "account_manager_id", "email", "phone",
]


@client_scoped("id")
class ClientListView(ModulePermissionRequiredMixin, ListView):
    model = Client
    template_name = "clients/client_list.html"
    context_object_name = "clients"
    paginate_by = 25
    permission_module = "client"
    permission_action = "view"

    def get_queryset(self):
        qs = Client.objects.select_related("account_manager").annotate(
            vehicle_count=Count("vehicles", distinct=True),
            driver_count=Count("drivers", distinct=True),
        )

        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(
                Q(client_name__icontains=query)
                | Q(client_code__icontains=query)
                | Q(legal_name__icontains=query)
                | Q(email__icontains=query)
                | Q(phone__icontains=query)
            )

        status = self.request.GET.get("status", "").strip()
        if status:
            qs = qs.filter(status=status)

        client_type = self.request.GET.get("client_type", "").strip()
        if client_type:
            qs = qs.filter(client_type=client_type)

        account_manager = self.request.GET.get("account_manager", "").strip()
        if account_manager:
            qs = qs.filter(account_manager_id=account_manager)

        return qs.order_by("client_name")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        q = self.request.GET.get("q", "")
        status = self.request.GET.get("status", "")
        client_type = self.request.GET.get("client_type", "")
        account_manager = self.request.GET.get("account_manager", "")
        context["current_filters"] = {
            "q": q, "status": status, "client_type": client_type, "account_manager": account_manager,
        }
        context["filter_fields"] = [
            {"type": "search", "name": "q", "label": "Search", "value": q, "col": 4,
             "placeholder": "Name, code, email, phone…"},
            {"type": "select", "name": "status", "label": "Status", "value": status, "col": 2,
             "choices": Client.Status.choices},
            {"type": "select", "name": "client_type", "label": "Type", "value": client_type, "col": 2,
             "choices": Client.ClientType.choices},
            {"type": "select", "name": "account_manager", "label": "Account Manager", "value": account_manager, "col": 3,
             "choices": [] if is_client_scoped(self.request.user) else [(str(u.id), u.get_full_name() or u.username) for u in User.objects.filter(managed_clients__isnull=False).distinct()]},
        ]
        querystring = self.request.GET.urlencode()
        context["export_url"] = reverse("clients:client_export") + (f"?{querystring}" if querystring else "")
        context["create_url"] = reverse("clients:client_create")
        return context


class ClientExportView(ClientListView):
    permission_module = "client"
    permission_action = "export"
    paginate_by = None

    def get(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="clients_export.csv"'
        writer = csv.writer(response)
        writer.writerow(
            ["Client Code", "Client Name", "Type", "Email", "Phone", "Account Manager",
             "Vehicles", "Drivers", "Status"]
        )
        for c in queryset:
            writer.writerow(
                [
                    c.client_code, c.client_name, c.get_client_type_display(), c.email, c.phone,
                    c.account_manager.get_full_name() if c.account_manager_id else "",
                    c.vehicle_count, c.driver_count, c.get_status_display(),
                ]
            )
        log_action(
            action=AuditLog.Action.EXPORT, module="client", entity="Client", entity_id="",
            new_value={"row_count": queryset.count()}, user=request.user, request=request,
        )
        return response


class ClientCreateView(ModulePermissionRequiredMixin, CreateView):
    model = Client
    form_class = ClientForm
    template_name = "clients/client_form.html"
    permission_module = "client"
    permission_action = "create"

    def form_valid(self, form):
        self.object = form.save(actor=self.request.user)
        log_action(
            action=AuditLog.Action.CREATE, module="client", entity="Client", entity_id=str(self.object.pk),
            new_value=model_to_dict_safe(self.object, CLIENT_AUDIT_FIELDS),
            user=self.request.user, request=self.request,
        )
        messages.success(self.request, f"Client '{self.object.client_name}' was created successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("clients:client_detail", kwargs={"uuid": self.object.uuid})


class ClientUpdateView(ModulePermissionRequiredMixin, UpdateView):
    model = Client
    form_class = ClientForm
    template_name = "clients/client_form.html"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "client"
    permission_action = "update"

    def form_valid(self, form):
        old_value = model_to_dict_safe(self.get_object(), CLIENT_AUDIT_FIELDS)
        self.object = form.save(actor=self.request.user)
        log_action(
            action=AuditLog.Action.UPDATE, module="client", entity="Client", entity_id=str(self.object.pk),
            old_value=old_value, new_value=model_to_dict_safe(self.object, CLIENT_AUDIT_FIELDS),
            user=self.request.user, request=self.request,
        )
        messages.success(self.request, f"Client '{self.object.client_name}' was updated successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("clients:client_detail", kwargs={"uuid": self.object.uuid})


CLIENT_TAB_MODULES = {
    "vehicles": "vehicle",
    "drivers": "driver",
    "trips": "trip",
    "branches": "branch",
    "contracts": "contract",
    "sites": "site",
    "documents": "document",
    "history": "audit_log",
}


@client_scoped("id")
class ClientDetailView(ModulePermissionRequiredMixin, DetailView):
    model = Client
    template_name = "clients/client_detail.html"
    context_object_name = "client"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "client"
    permission_action = "view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        active_tab = self.request.GET.get("tab", "overview")
        context["active_tab"] = active_tab
        context["vehicles"] = self.object.vehicles.select_related("vehicle_type", "current_driver")[:50]
        context["drivers"] = self.object.drivers.all()[:50]
        context["branches"] = self.object.branches.all()[:50]
        context["contracts"] = self.object.contracts.all()[:50]
        context["sites"] = self.object.sites.select_related("branch")[:50]
        context["vehicle_count"] = self.object.vehicles.count()
        context["driver_count"] = self.object.drivers.count()
        context["branch_count"] = self.object.branches.count()
        context["site_count"] = self.object.sites.count()
        context["active_contract_count"] = self.object.contracts.filter(status="ACTIVE").count()

        trips = self.object.trips.select_related("vehicle", "driver", "origin_site", "destination_site").order_by("-scheduled_start")
        context["trips"] = trips[:50]
        context["trip_count"] = trips.count()
        context["active_trip_count"] = trips.filter(
            status__in=["ASSIGNED", "DISPATCHED", "IN_PROGRESS", "DELAYED"]
        ).count()
        context["completed_trip_count"] = trips.filter(status="COMPLETED").count()

        context["recent_activity"] = AuditLog.objects.filter(
            module="client", entity="Client", entity_id=str(self.object.pk)
        ).select_related("user").order_by("-timestamp")[:20]
        context["documents"] = get_documents_for(self.object)

        base_url = reverse("clients:client_detail", kwargs={"uuid": self.object.uuid})
        context["tab_list"] = [
            {"id": "overview", "label": "Overview", "icon": "info-circle", "url": base_url},
            {"id": "vehicles", "label": "Vehicles", "icon": "truck-front", "url": f"{base_url}?tab=vehicles"},
            {"id": "drivers", "label": "Drivers", "icon": "person-badge", "url": f"{base_url}?tab=drivers"},
            {"id": "trips", "label": "Trips", "icon": "signpost-split", "url": f"{base_url}?tab=trips"},
            {"id": "branches", "label": "Branches", "icon": "diagram-3", "url": f"{base_url}?tab=branches"},
            {"id": "contracts", "label": "Contracts", "icon": "file-earmark-text", "url": f"{base_url}?tab=contracts"},
            {"id": "sites", "label": "Sites", "icon": "geo-alt", "url": f"{base_url}?tab=sites"},
            {"id": "documents", "label": "Documents", "icon": "file-earmark-check", "url": f"{base_url}?tab=documents"},
            {"id": "history", "label": "History", "icon": "clock-history", "url": f"{base_url}?tab=history"},
        ]
        restrict_detail_tabs(self.request.user, context, CLIENT_TAB_MODULES)
        return context


def _set_client_status(request, uuid, new_status, note):
    client = get_object_or_404(Client, uuid=uuid)
    old_status = client.status
    client.status = new_status
    client.updated_by = request.user
    client.save(update_fields=["status", "updated_by", "updated_at"])
    log_action(
        action=AuditLog.Action.UPDATE, module="client", entity="Client", entity_id=str(client.pk),
        old_value={"status": old_status}, new_value={"status": new_status},
        user=request.user, request=request,
    )
    messages.success(request, f"{client.client_name} {note}.")
    return redirect("clients:client_detail", uuid=client.uuid)


@require_POST
@module_permission_required("client", "archive")
def client_deactivate(request, uuid):
    return _set_client_status(request, uuid, Client.Status.INACTIVE, "has been deactivated")


@require_POST
@module_permission_required("client", "archive")
def client_activate(request, uuid):
    return _set_client_status(request, uuid, Client.Status.ACTIVE, "has been reactivated")
