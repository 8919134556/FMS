"""Contract CRUD, list/filter/export.

Mirrors apps.clients.views / apps.locations.views / apps.vendors.views (same
permission mixins, same audit logging shape).
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
from apps.contracts.forms import ContractForm
from apps.contracts.models import Contract
from apps.core.permissions import ModulePermissionRequiredMixin, module_permission_required, restrict_detail_tabs
from apps.core.scoping import client_scoped, is_client_scoped, scope_queryset
from apps.core.utils import model_to_dict_safe
from apps.vendors.models import Vendor

CONTRACT_AUDIT_FIELDS = [
    "contract_number", "contract_type", "status", "client_id", "vendor_id", "start_date", "end_date",
]


@client_scoped("client_id")
class ContractListView(ModulePermissionRequiredMixin, ListView):
    model = Contract
    template_name = "contracts/contract_list.html"
    context_object_name = "contracts"
    paginate_by = 25
    permission_module = "contract"
    permission_action = "view"

    def get_queryset(self):
        qs = Contract.objects.select_related("client", "vendor").all()

        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(
                Q(contract_number__icontains=query)
                | Q(client__client_name__icontains=query)
                | Q(vendor__vendor_name__icontains=query)
            )

        status = self.request.GET.get("status", "").strip()
        if status:
            qs = qs.filter(status=status)

        contract_type = self.request.GET.get("contract_type", "").strip()
        if contract_type:
            qs = qs.filter(contract_type=contract_type)

        client = self.request.GET.get("client", "").strip()
        if client:
            qs = qs.filter(client_id=client)

        vendor = self.request.GET.get("vendor", "").strip()
        if vendor:
            qs = qs.filter(vendor_id=vendor)

        return qs.order_by("-created_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        q = self.request.GET.get("q", "")
        status = self.request.GET.get("status", "")
        contract_type = self.request.GET.get("contract_type", "")
        client = self.request.GET.get("client", "")
        vendor = self.request.GET.get("vendor", "")
        context["current_filters"] = {
            "q": q, "status": status, "contract_type": contract_type, "client": client, "vendor": vendor,
        }
        context["filter_fields"] = [
            {"type": "search", "name": "q", "label": "Search", "value": q, "col": 3,
             "placeholder": "Contract #, client, vendor…"},
            {"type": "select", "name": "status", "label": "Status", "value": status, "col": 2,
             "choices": Contract.Status.choices},
            {"type": "select", "name": "contract_type", "label": "Type", "value": contract_type, "col": 2,
             "choices": Contract.ContractType.choices},
            {"type": "select", "name": "client", "label": "Client", "value": client, "col": 2,
             "choices": [(str(c.id), c.client_name) for c in scope_queryset(Client.objects.filter(status=Client.Status.ACTIVE), self.request.user, "id")]},
            {"type": "select", "name": "vendor", "label": "Vendor", "value": vendor, "col": 3,
             "choices": [(str(v.id), v.vendor_name) for v in Vendor.objects.filter(status=Vendor.Status.ACTIVE) if not is_client_scoped(self.request.user)]},
        ]
        querystring = self.request.GET.urlencode()
        context["export_url"] = reverse("contracts:contract_export") + (f"?{querystring}" if querystring else "")
        context["create_url"] = reverse("contracts:contract_create")
        return context


class ContractExportView(ContractListView):
    permission_module = "contract"
    permission_action = "export"
    paginate_by = None

    def get(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="contracts_export.csv"'
        writer = csv.writer(response)
        writer.writerow(
            ["Contract #", "Type", "Client", "Vendor", "Start", "End", "Value", "Status"]
        )
        for c in queryset:
            writer.writerow(
                [
                    c.contract_number, c.get_contract_type_display(),
                    c.client.client_name if c.client_id else "", c.vendor.vendor_name if c.vendor_id else "",
                    c.start_date or "", c.end_date or "", c.contract_value if c.contract_value is not None else "",
                    c.get_status_display(),
                ]
            )
        log_action(
            action=AuditLog.Action.EXPORT, module="contract", entity="Contract", entity_id="",
            new_value={"row_count": queryset.count()}, user=request.user, request=request,
        )
        return response


class ContractCreateView(ModulePermissionRequiredMixin, CreateView):
    model = Contract
    form_class = ContractForm
    template_name = "contracts/contract_form.html"
    permission_module = "contract"
    permission_action = "create"

    def form_valid(self, form):
        self.object = form.save(actor=self.request.user)
        log_action(
            action=AuditLog.Action.CREATE, module="contract", entity="Contract", entity_id=str(self.object.pk),
            new_value=model_to_dict_safe(self.object, CONTRACT_AUDIT_FIELDS),
            user=self.request.user, request=self.request,
        )
        messages.success(self.request, f"Contract '{self.object.contract_number}' was created successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("contracts:contract_detail", kwargs={"uuid": self.object.uuid})


class ContractUpdateView(ModulePermissionRequiredMixin, UpdateView):
    model = Contract
    form_class = ContractForm
    template_name = "contracts/contract_form.html"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "contract"
    permission_action = "update"

    def form_valid(self, form):
        old_value = model_to_dict_safe(self.get_object(), CONTRACT_AUDIT_FIELDS)
        self.object = form.save(actor=self.request.user)
        log_action(
            action=AuditLog.Action.UPDATE, module="contract", entity="Contract", entity_id=str(self.object.pk),
            old_value=old_value, new_value=model_to_dict_safe(self.object, CONTRACT_AUDIT_FIELDS),
            user=self.request.user, request=self.request,
        )
        messages.success(self.request, f"Contract '{self.object.contract_number}' was updated successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("contracts:contract_detail", kwargs={"uuid": self.object.uuid})


CONTRACT_TAB_MODULES = {
    "vehicles": "vehicle",
    "history": "audit_log",
}


@client_scoped("client_id")
class ContractDetailView(ModulePermissionRequiredMixin, DetailView):
    model = Contract
    template_name = "contracts/contract_detail.html"
    context_object_name = "contract"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "contract"
    permission_action = "view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        active_tab = self.request.GET.get("tab", "overview")
        context["active_tab"] = active_tab
        context["contract_vehicles"] = self.object.vehicles.select_related("vehicle_type").order_by("registration_number")[:25]
        context["recent_activity"] = AuditLog.objects.filter(
            module="contract", entity="Contract", entity_id=str(self.object.pk)
        ).select_related("user").order_by("-timestamp")[:20]

        base_url = reverse("contracts:contract_detail", kwargs={"uuid": self.object.uuid})
        context["tab_list"] = [
            {"id": "overview", "label": "Overview", "icon": "info-circle", "url": base_url},
            {"id": "vehicles", "label": "Vehicles", "icon": "truck-front", "url": f"{base_url}?tab=vehicles"},
            {"id": "history", "label": "History", "icon": "clock-history", "url": f"{base_url}?tab=history"},
        ]
        restrict_detail_tabs(self.request.user, context, CONTRACT_TAB_MODULES)
        return context


def _set_contract_status(request, uuid, new_status, note):
    contract = get_object_or_404(Contract, uuid=uuid)
    old_status = contract.status
    contract.status = new_status
    contract.updated_by = request.user
    contract.save(update_fields=["status", "updated_by", "updated_at"])
    log_action(
        action=AuditLog.Action.UPDATE, module="contract", entity="Contract", entity_id=str(contract.pk),
        old_value={"status": old_status}, new_value={"status": new_status},
        user=request.user, request=request,
    )
    messages.success(request, f"{contract.contract_number} {note}.")
    return redirect("contracts:contract_detail", uuid=contract.uuid)


@require_POST
@module_permission_required("contract", "archive")
def contract_terminate(request, uuid):
    return _set_contract_status(request, uuid, Contract.Status.TERMINATED, "has been terminated")


@require_POST
@module_permission_required("contract", "update")
def contract_renew(request, uuid):
    return _set_contract_status(request, uuid, Contract.Status.RENEWED, "has been marked as renewed")
