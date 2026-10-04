"""Driver CRUD, list/filter/export, and the vehicle-assignment workflow.

Mirrors apps.vehicles.views module-for-module (same permission mixins, same
audit logging shape, same assignment service) so Drivers behaves like the
same product as Vehicles rather than a separately-invented module. Trip
history, performance scoring, and a dedicated documents tab arrive when
Trips/Documents are built.
"""

import csv
import datetime

from django.contrib import messages
from django.db.models import Prefetch, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.clients.models import Client
from apps.core.permissions import ModulePermissionRequiredMixin, module_permission_required, restrict_detail_tabs
from apps.core.scoping import client_scoped, is_client_scoped, scope_queryset
from apps.core.utils import model_to_dict_safe
from apps.documents.services import get_documents_for
from apps.drivers.forms import DriverAssignVehicleForm, DriverForm
from apps.drivers.models import Driver
from apps.locations.models import Branch
from apps.vehicles import services
from apps.vehicles.models import Vehicle, VehicleDriverAssignment

DRIVER_AUDIT_FIELDS = [
    "employee_id", "first_name", "last_name", "mobile_number", "license_number",
    "employment_status", "client_id", "branch_id",
]


@client_scoped("client_id")
class DriverListView(ModulePermissionRequiredMixin, ListView):
    model = Driver
    template_name = "drivers/driver_list.html"
    context_object_name = "drivers"
    paginate_by = 25
    permission_module = "driver"
    permission_action = "view"

    def get_queryset(self):
        qs = Driver.objects.select_related("client", "branch").prefetch_related(
            Prefetch(
                "current_vehicles",
                queryset=Vehicle.objects.select_related("vehicle_type"),
                to_attr="current_vehicle_list",
            )
        ).all()

        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(
                Q(first_name__icontains=query)
                | Q(last_name__icontains=query)
                | Q(employee_id__icontains=query)
                | Q(license_number__icontains=query)
                | Q(mobile_number__icontains=query)
                | Q(email__icontains=query)
            )

        status = self.request.GET.get("status", "").strip()
        if status:
            qs = qs.filter(employment_status=status)

        client = self.request.GET.get("client", "").strip()
        if client:
            qs = qs.filter(client_id=client)

        branch = self.request.GET.get("branch", "").strip()
        if branch:
            qs = qs.filter(branch_id=branch)

        vehicle = self.request.GET.get("vehicle", "").strip()
        if vehicle == "assigned":
            qs = qs.filter(current_vehicles__isnull=False).distinct()
        elif vehicle == "unassigned":
            qs = qs.filter(current_vehicles__isnull=True)

        license_status = self.request.GET.get("license_status", "").strip()
        if license_status:
            today = timezone.now().date()
            warning_cutoff = today + datetime.timedelta(days=Driver.LICENSE_EXPIRY_WARNING_DAYS)
            if license_status == "EXPIRED":
                qs = qs.filter(license_expiry_date__lt=today)
            elif license_status == "EXPIRING_SOON":
                qs = qs.filter(license_expiry_date__gte=today, license_expiry_date__lte=warning_cutoff)
            elif license_status == "VALID":
                qs = qs.filter(Q(license_expiry_date__gt=warning_cutoff) | Q(license_expiry_date__isnull=True))

        return qs.order_by("first_name", "last_name")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        q = self.request.GET.get("q", "")
        status = self.request.GET.get("status", "")
        client = self.request.GET.get("client", "")
        branch = self.request.GET.get("branch", "")
        vehicle = self.request.GET.get("vehicle", "")
        license_status = self.request.GET.get("license_status", "")
        context["current_filters"] = {
            "q": q, "status": status, "client": client, "branch": branch,
            "vehicle": vehicle, "license_status": license_status,
        }
        context["filter_fields"] = [
            {"type": "search", "name": "q", "label": "Search", "value": q, "col": 3,
             "placeholder": "Name, employee ID, phone, license…"},
            {"type": "select", "name": "status", "label": "Status", "value": status, "col": 2,
             "choices": Driver.EmploymentStatus.choices},
            {"type": "select", "name": "license_status", "label": "License Status", "value": license_status, "col": 2,
             "choices": [("VALID", "Valid"), ("EXPIRING_SOON", "Expiring Soon"), ("EXPIRED", "Expired")]},
            {"type": "select", "name": "client", "label": "Client", "value": client, "col": 2,
             "choices": [(str(c.id), c.client_name) for c in scope_queryset(Client.objects.filter(status=Client.Status.ACTIVE), self.request.user, "id")]},
            {"type": "select", "name": "branch", "label": "Branch", "value": branch, "col": 2,
             "choices": [(str(b.id), b.name) for b in Branch.objects.filter(status=Branch.Status.ACTIVE) if not is_client_scoped(self.request.user)]},
            {"type": "select", "name": "vehicle", "label": "Assigned Vehicle", "value": vehicle, "col": 2,
             "choices": [("assigned", "Has a vehicle"), ("unassigned", "No vehicle")]},
        ]
        querystring = self.request.GET.urlencode()
        context["export_url"] = reverse("drivers:driver_export") + (f"?{querystring}" if querystring else "")
        context["create_url"] = reverse("drivers:driver_create")
        return context


class DriverExportView(DriverListView):
    permission_module = "driver"
    permission_action = "export"
    paginate_by = None

    def get(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="drivers_export.csv"'
        writer = csv.writer(response)
        writer.writerow(
            ["Employee ID", "Name", "Mobile", "Email", "License Number", "License Expiry",
             "Client", "Branch", "Assigned Vehicle", "Status"]
        )
        for d in queryset:
            current_vehicle = d.current_vehicle_list[0] if d.current_vehicle_list else None
            writer.writerow(
                [
                    d.employee_id, d.get_full_name(), d.mobile_number, d.email,
                    d.license_number, d.license_expiry_date or "",
                    d.client.client_name if d.client_id else "",
                    d.branch.name if d.branch_id else "",
                    current_vehicle.registration_number if current_vehicle else "",
                    d.get_employment_status_display(),
                ]
            )
        log_action(
            action=AuditLog.Action.EXPORT, module="driver", entity="Driver", entity_id="",
            new_value={"row_count": queryset.count()}, user=request.user, request=request,
        )
        return response


class DriverCreateView(ModulePermissionRequiredMixin, CreateView):
    model = Driver
    form_class = DriverForm
    template_name = "drivers/driver_form.html"
    permission_module = "driver"
    permission_action = "create"

    def form_valid(self, form):
        self.object = form.save(actor=self.request.user)
        log_action(
            action=AuditLog.Action.CREATE, module="driver", entity="Driver", entity_id=str(self.object.pk),
            new_value=model_to_dict_safe(self.object, DRIVER_AUDIT_FIELDS),
            user=self.request.user, request=self.request,
        )
        messages.success(self.request, f"Driver '{self.object.get_full_name()}' was created successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("drivers:driver_detail", kwargs={"uuid": self.object.uuid})


class DriverUpdateView(ModulePermissionRequiredMixin, UpdateView):
    model = Driver
    form_class = DriverForm
    template_name = "drivers/driver_form.html"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "driver"
    permission_action = "update"

    def form_valid(self, form):
        old_value = model_to_dict_safe(self.get_object(), DRIVER_AUDIT_FIELDS)
        self.object = form.save(actor=self.request.user)
        log_action(
            action=AuditLog.Action.UPDATE, module="driver", entity="Driver", entity_id=str(self.object.pk),
            old_value=old_value, new_value=model_to_dict_safe(self.object, DRIVER_AUDIT_FIELDS),
            user=self.request.user, request=self.request,
        )
        messages.success(self.request, f"Driver '{self.object.get_full_name()}' was updated successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("drivers:driver_detail", kwargs={"uuid": self.object.uuid})


DRIVER_TAB_MODULES = {
    "documents": "document",
    "vehicles": "vehicle",
    "trips": "trip",
    "performance": "trip",
    "history": "audit_log",
}


@client_scoped("client_id")
class DriverDetailView(ModulePermissionRequiredMixin, DetailView):
    model = Driver
    template_name = "drivers/driver_detail.html"
    context_object_name = "driver"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "driver"
    permission_action = "view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        active_tab = self.request.GET.get("tab", "overview")
        context["active_tab"] = active_tab
        context["current_vehicle"] = self.object.current_vehicles.select_related("vehicle_type").first()
        context["assignment_history"] = self.object.vehicle_assignments.select_related(
            "vehicle", "assigned_by"
        ).order_by("-start_date")[:20]
        context["recent_activity"] = AuditLog.objects.filter(
            module="driver", entity="Driver", entity_id=str(self.object.pk)
        ).select_related("user").order_by("-timestamp")[:20]
        context["assign_vehicle_form"] = DriverAssignVehicleForm()

        trips = self.object.trips.select_related("client", "vehicle", "origin_site", "destination_site").order_by("-scheduled_start")
        context["trips"] = trips[:50]
        completed_trips = trips.filter(status="COMPLETED")
        completed_count = completed_trips.count()
        context["trip_count"] = trips.count()
        context["completed_trip_count"] = completed_count
        context["cancelled_trip_count"] = trips.filter(status="CANCELLED").count()
        context["trip_total_distance"] = completed_trips.aggregate(total=Sum("actual_distance"))["total"] or 0
        if completed_count:
            on_time_count = sum(1 for t in completed_trips if t.is_on_time)
            context["on_time_percentage"] = round(on_time_count / completed_count * 100, 1)
        else:
            context["on_time_percentage"] = None

        context["documents"] = get_documents_for(self.object)

        base_url = reverse("drivers:driver_detail", kwargs={"uuid": self.object.uuid})
        context["tab_list"] = [
            {"id": "overview", "label": "Overview", "icon": "info-circle", "url": base_url},
            {"id": "license", "label": "License", "icon": "card-heading", "url": f"{base_url}?tab=license"},
            {"id": "documents", "label": "Documents", "icon": "file-earmark-text", "url": f"{base_url}?tab=documents"},
            {"id": "vehicles", "label": "Vehicles", "icon": "truck-front", "url": f"{base_url}?tab=vehicles"},
            {"id": "trips", "label": "Trips", "icon": "map", "url": f"{base_url}?tab=trips"},
            {"id": "performance", "label": "Performance", "icon": "graph-up", "url": f"{base_url}?tab=performance"},
            {"id": "history", "label": "History", "icon": "clock-history", "url": f"{base_url}?tab=history"},
        ]
        restrict_detail_tabs(self.request.user, context, DRIVER_TAB_MODULES)
        return context


@require_POST
@module_permission_required("assignment", "create")
def driver_assign_vehicle(request, uuid):
    driver = get_object_or_404(Driver, uuid=uuid)
    form = DriverAssignVehicleForm(request.POST)
    if form.is_valid():
        services.assign_driver(
            vehicle=form.cleaned_data["vehicle"],
            driver=driver,
            start_date=form.cleaned_data["start_date"],
            assignment_type=form.cleaned_data["assignment_type"],
            primary_driver=form.cleaned_data["primary_driver"],
            remarks=form.cleaned_data["remarks"],
            assigned_by=request.user,
            request=request,
        )
        messages.success(request, f"{form.cleaned_data['vehicle'].registration_number} was assigned to {driver.get_full_name()}.")
    else:
        error_text = "; ".join(f"{field}: {', '.join(errs)}" for field, errs in form.errors.items())
        messages.error(request, f"Could not assign vehicle — {error_text}")
    return redirect(reverse("drivers:driver_detail", kwargs={"uuid": driver.uuid}) + "?tab=vehicles")


@require_POST
@module_permission_required("assignment", "update")
def driver_unassign_vehicle(request, uuid, assignment_id):
    driver = get_object_or_404(Driver, uuid=uuid)
    assignment = get_object_or_404(VehicleDriverAssignment, pk=assignment_id, driver=driver)
    if assignment.status == VehicleDriverAssignment.Status.ACTIVE:
        services.end_assignment(assignment=assignment, ended_by=request.user, request=request)
        messages.success(request, f"Assignment for {assignment.vehicle.registration_number} was ended.")
    return redirect(reverse("drivers:driver_detail", kwargs={"uuid": driver.uuid}) + "?tab=vehicles")


def _set_driver_status(request, uuid, new_status, note):
    driver = get_object_or_404(Driver, uuid=uuid)
    old_status = driver.employment_status
    driver.employment_status = new_status
    driver.updated_by = request.user
    driver.save(update_fields=["employment_status", "updated_by", "updated_at"])
    log_action(
        action=AuditLog.Action.UPDATE, module="driver", entity="Driver", entity_id=str(driver.pk),
        old_value={"employment_status": old_status}, new_value={"employment_status": new_status},
        user=request.user, request=request,
    )
    messages.success(request, f"{driver.get_full_name()} {note}.")
    return redirect("drivers:driver_detail", uuid=driver.uuid)


@require_POST
@module_permission_required("driver", "archive")
def driver_deactivate(request, uuid):
    return _set_driver_status(request, uuid, Driver.EmploymentStatus.INACTIVE, "has been deactivated")


@require_POST
@module_permission_required("driver", "archive")
def driver_activate(request, uuid):
    return _set_driver_status(request, uuid, Driver.EmploymentStatus.ACTIVE, "has been reactivated")
