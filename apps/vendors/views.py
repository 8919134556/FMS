"""Vendor CRUD, list/filter/export.

Mirrors apps.clients.views / apps.locations.views (same permission mixins,
same audit logging shape) so Vendors behaves like the same product rather
than a separately-invented module.
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
from apps.core.permissions import ModulePermissionRequiredMixin, module_permission_required, restrict_detail_tabs
from apps.core.utils import model_to_dict_safe
from apps.vendors.forms import VendorForm
from apps.vendors.models import Vendor

VENDOR_AUDIT_FIELDS = ["vendor_code", "vendor_name", "vendor_type", "status"]


class VendorListView(ModulePermissionRequiredMixin, ListView):
    model = Vendor
    template_name = "vendors/vendor_list.html"
    context_object_name = "vendors"
    paginate_by = 25
    permission_module = "vendor"
    permission_action = "view"

    def get_queryset(self):
        qs = Vendor.objects.all()

        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(
                Q(vendor_name__icontains=query)
                | Q(vendor_code__icontains=query)
                | Q(contact_person__icontains=query)
                | Q(email__icontains=query)
                | Q(phone__icontains=query)
            )

        status = self.request.GET.get("status", "").strip()
        if status:
            qs = qs.filter(status=status)

        vendor_type = self.request.GET.get("vendor_type", "").strip()
        if vendor_type:
            qs = qs.filter(vendor_type=vendor_type)

        return qs.order_by("vendor_name")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        q = self.request.GET.get("q", "")
        status = self.request.GET.get("status", "")
        vendor_type = self.request.GET.get("vendor_type", "")
        context["current_filters"] = {"q": q, "status": status, "vendor_type": vendor_type}
        context["filter_fields"] = [
            {"type": "search", "name": "q", "label": "Search", "value": q, "col": 4,
             "placeholder": "Vendor name, code, contact, email…"},
            {"type": "select", "name": "status", "label": "Status", "value": status, "col": 2,
             "choices": Vendor.Status.choices},
            {"type": "select", "name": "vendor_type", "label": "Type", "value": vendor_type, "col": 2,
             "choices": Vendor.VendorType.choices},
        ]
        querystring = self.request.GET.urlencode()
        context["export_url"] = reverse("vendors:vendor_export") + (f"?{querystring}" if querystring else "")
        context["create_url"] = reverse("vendors:vendor_create")
        return context


class VendorExportView(VendorListView):
    permission_module = "vendor"
    permission_action = "export"
    paginate_by = None

    def get(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="vendors_export.csv"'
        writer = csv.writer(response)
        writer.writerow(["Vendor Code", "Vendor Name", "Type", "Contact", "Email", "Phone", "Status"])
        for v in queryset:
            writer.writerow(
                [v.vendor_code, v.vendor_name, v.get_vendor_type_display(), v.contact_person, v.email, v.phone,
                 v.get_status_display()]
            )
        log_action(
            action=AuditLog.Action.EXPORT, module="vendor", entity="Vendor", entity_id="",
            new_value={"row_count": queryset.count()}, user=request.user, request=request,
        )
        return response


class VendorCreateView(ModulePermissionRequiredMixin, CreateView):
    model = Vendor
    form_class = VendorForm
    template_name = "vendors/vendor_form.html"
    permission_module = "vendor"
    permission_action = "create"

    def form_valid(self, form):
        self.object = form.save(actor=self.request.user)
        log_action(
            action=AuditLog.Action.CREATE, module="vendor", entity="Vendor", entity_id=str(self.object.pk),
            new_value=model_to_dict_safe(self.object, VENDOR_AUDIT_FIELDS),
            user=self.request.user, request=self.request,
        )
        messages.success(self.request, f"Vendor '{self.object.vendor_name}' was created successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("vendors:vendor_detail", kwargs={"uuid": self.object.uuid})


class VendorUpdateView(ModulePermissionRequiredMixin, UpdateView):
    model = Vendor
    form_class = VendorForm
    template_name = "vendors/vendor_form.html"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "vendor"
    permission_action = "update"

    def form_valid(self, form):
        old_value = model_to_dict_safe(self.get_object(), VENDOR_AUDIT_FIELDS)
        self.object = form.save(actor=self.request.user)
        log_action(
            action=AuditLog.Action.UPDATE, module="vendor", entity="Vendor", entity_id=str(self.object.pk),
            old_value=old_value, new_value=model_to_dict_safe(self.object, VENDOR_AUDIT_FIELDS),
            user=self.request.user, request=self.request,
        )
        messages.success(self.request, f"Vendor '{self.object.vendor_name}' was updated successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("vendors:vendor_detail", kwargs={"uuid": self.object.uuid})


VENDOR_TAB_MODULES = {
    "vehicles": "vehicle",
    "contracts": "contract",
    "history": "audit_log",
}


class VendorDetailView(ModulePermissionRequiredMixin, DetailView):
    model = Vendor
    template_name = "vendors/vendor_detail.html"
    context_object_name = "vendor"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "vendor"
    permission_action = "view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        active_tab = self.request.GET.get("tab", "overview")
        context["active_tab"] = active_tab
        context["vendor_vehicles"] = self.object.vehicles.select_related("vehicle_type").order_by("registration_number")[:25]
        context["vendor_contracts"] = self.object.contracts.select_related("client").order_by("-created_at")[:25]
        context["recent_activity"] = AuditLog.objects.filter(
            module="vendor", entity="Vendor", entity_id=str(self.object.pk)
        ).select_related("user").order_by("-timestamp")[:20]

        base_url = reverse("vendors:vendor_detail", kwargs={"uuid": self.object.uuid})
        context["tab_list"] = [
            {"id": "overview", "label": "Overview", "icon": "info-circle", "url": base_url},
            {"id": "vehicles", "label": "Vehicles", "icon": "truck-front", "url": f"{base_url}?tab=vehicles"},
            {"id": "contracts", "label": "Contracts", "icon": "file-earmark-text", "url": f"{base_url}?tab=contracts"},
            {"id": "history", "label": "History", "icon": "clock-history", "url": f"{base_url}?tab=history"},
        ]
        restrict_detail_tabs(self.request.user, context, VENDOR_TAB_MODULES)
        return context


def _set_vendor_status(request, uuid, new_status, note):
    vendor = get_object_or_404(Vendor, uuid=uuid)
    old_status = vendor.status
    vendor.status = new_status
    vendor.updated_by = request.user
    vendor.save(update_fields=["status", "updated_by", "updated_at"])
    log_action(
        action=AuditLog.Action.UPDATE, module="vendor", entity="Vendor", entity_id=str(vendor.pk),
        old_value={"status": old_status}, new_value={"status": new_status},
        user=request.user, request=request,
    )
    messages.success(request, f"{vendor.vendor_name} {note}.")
    return redirect("vendors:vendor_detail", uuid=vendor.uuid)


@require_POST
@module_permission_required("vendor", "archive")
def vendor_deactivate(request, uuid):
    return _set_vendor_status(request, uuid, Vendor.Status.INACTIVE, "has been deactivated")


@require_POST
@module_permission_required("vendor", "archive")
def vendor_activate(request, uuid):
    return _set_vendor_status(request, uuid, Vendor.Status.ACTIVE, "has been reactivated")
