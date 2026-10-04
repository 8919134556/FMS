"""Vehicle CRUD, list/filter/export, and the driver-assignment workflow.

Mounted at ``/fleet/vehicles/`` (see ``apps/vehicles/urls.py``).
"""

import csv

from django.contrib import messages
from django.db.models import Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.core.permissions import ModulePermissionRequiredMixin, module_permission_required, restrict_detail_tabs
from apps.core.scoping import client_scoped, scope_queryset
from apps.core.utils import model_to_dict_safe
from apps.documents.services import get_documents_for
from apps.tracking.models import VehicleCurrentTelemetry
from apps.tracking.services import connection_status_for
from apps.vehicles import services
from apps.vehicles.forms import VehicleAssignDriverForm, VehicleForm, VehicleTypeForm
from apps.vehicles.models import Vehicle, VehicleCategory, VehicleDriverAssignment, VehicleType

VEHICLE_AUDIT_FIELDS = [
    "registration_number", "vehicle_code", "make", "model", "status", "availability_status",
    "vehicle_type_id", "fuel_type_id", "client_id", "branch_id",
    # Changes how this vehicle's trips are split — worth an audit trail.
    "trip_closure_minutes", "trip_validation_records", "trip_min_moving_records",
    "trip_min_speed_kmh", "trip_min_distance_m", "idle_alert_minutes",
]


@client_scoped("client_id")
class VehicleListView(ModulePermissionRequiredMixin, ListView):
    model = Vehicle
    template_name = "vehicles/vehicle_list.html"
    context_object_name = "vehicles"
    paginate_by = 25
    permission_module = "vehicle"
    permission_action = "view"

    def get_queryset(self):
        qs = Vehicle.objects.select_related(
            "vehicle_type", "fuel_type", "client", "branch", "current_driver"
        ).all()

        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(
                Q(registration_number__icontains=query)
                | Q(vehicle_code__icontains=query)
                | Q(make__icontains=query)
                | Q(model__icontains=query)
                | Q(vin__icontains=query)
            )

        status = self.request.GET.get("status", "").strip()
        if status:
            qs = qs.filter(status=status)

        availability = self.request.GET.get("availability", "").strip()
        if availability:
            qs = qs.filter(availability_status=availability)

        vehicle_type = self.request.GET.get("vehicle_type", "").strip()
        if vehicle_type:
            qs = qs.filter(vehicle_type_id=vehicle_type)

        client = self.request.GET.get("client", "").strip()
        if client:
            qs = qs.filter(client_id=client)

        branch = self.request.GET.get("branch", "").strip()
        if branch:
            qs = qs.filter(branch_id=branch)

        gps = self.request.GET.get("gps", "").strip()
        if gps == "online":
            qs = qs.filter(tracking_device__isnull=False, tracking_device__status="ACTIVE")
        elif gps == "none":
            qs = qs.filter(tracking_device__isnull=True)

        return qs.order_by("registration_number")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        q = self.request.GET.get("q", "")
        status = self.request.GET.get("status", "")
        availability = self.request.GET.get("availability", "")
        vehicle_type = self.request.GET.get("vehicle_type", "")
        context["current_filters"] = {
            "q": q, "status": status, "availability": availability, "vehicle_type": vehicle_type,
        }
        context["filter_fields"] = [
            {"type": "search", "name": "q", "label": "Search", "value": q, "col": 3,
             "placeholder": "Registration, code, make, model, VIN…"},
            {"type": "select", "name": "status", "label": "Status", "value": status, "col": 2,
             "choices": Vehicle.Status.choices},
            {"type": "select", "name": "availability", "label": "Availability", "value": availability, "col": 2,
             "choices": Vehicle.AvailabilityStatus.choices},
            {"type": "select", "name": "vehicle_type", "label": "Vehicle Type", "value": vehicle_type, "col": 2,
             "choices": [(str(t.id), t.name) for t in VehicleType.objects.filter(is_active=True)]},
        ]
        querystring = self.request.GET.urlencode()
        context["export_url"] = reverse("vehicles:vehicle_export") + (f"?{querystring}" if querystring else "")
        context["create_url"] = reverse("vehicles:vehicle_create")
        return context


class VehicleExportView(VehicleListView):
    permission_module = "vehicle"
    permission_action = "export"
    paginate_by = None

    def get(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="vehicles_export.csv"'
        writer = csv.writer(response)
        writer.writerow(
            ["Registration", "Code", "Make", "Model", "Type", "Fuel", "Client", "Branch",
             "Driver", "Status", "Availability", "Odometer"]
        )
        for v in queryset:
            writer.writerow(
                [
                    v.registration_number, v.vehicle_code, v.make, v.model,
                    v.vehicle_type.name if v.vehicle_type_id else "",
                    v.fuel_type.name if v.fuel_type_id else "",
                    v.client.client_name if v.client_id else "",
                    v.branch.name if v.branch_id else "",
                    v.current_driver.get_full_name() if v.current_driver_id else "",
                    v.get_status_display(), v.get_availability_status_display(),
                    v.odometer_reading,
                ]
            )
        log_action(
            action=AuditLog.Action.EXPORT, module="vehicle", entity="Vehicle", entity_id="",
            new_value={"row_count": queryset.count()}, user=request.user, request=request,
        )
        return response


class VehicleCreateView(ModulePermissionRequiredMixin, CreateView):
    model = Vehicle
    form_class = VehicleForm
    template_name = "vehicles/vehicle_form.html"
    permission_module = "vehicle"
    permission_action = "create"

    def get_initial(self):
        # "Add Vehicle" from a client's page arrives as ?client=<id> so the
        # vehicle is registered under that client without re-selecting it.
        initial = super().get_initial()
        client_id = self.request.GET.get("client", "").strip()
        if client_id.isdigit():
            initial["client"] = client_id
        return initial

    def form_valid(self, form):
        self.object = form.save(actor=self.request.user)
        log_action(
            action=AuditLog.Action.CREATE, module="vehicle", entity="Vehicle", entity_id=str(self.object.pk),
            new_value=model_to_dict_safe(self.object, VEHICLE_AUDIT_FIELDS),
            user=self.request.user, request=self.request,
        )
        messages.success(self.request, f"Vehicle '{self.object.registration_number}' was created successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("vehicles:vehicle_detail", kwargs={"uuid": self.object.uuid})


class VehicleUpdateView(ModulePermissionRequiredMixin, UpdateView):
    model = Vehicle
    form_class = VehicleForm
    template_name = "vehicles/vehicle_form.html"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "vehicle"
    permission_action = "update"

    def form_valid(self, form):
        old_value = model_to_dict_safe(self.get_object(), VEHICLE_AUDIT_FIELDS)
        self.object = form.save(actor=self.request.user)
        log_action(
            action=AuditLog.Action.UPDATE, module="vehicle", entity="Vehicle", entity_id=str(self.object.pk),
            old_value=old_value, new_value=model_to_dict_safe(self.object, VEHICLE_AUDIT_FIELDS),
            user=self.request.user, request=self.request,
        )
        messages.success(self.request, f"Vehicle '{self.object.registration_number}' was updated successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("vehicles:vehicle_detail", kwargs={"uuid": self.object.uuid})


VEHICLE_TAB_MODULES = {
    "driver": "driver",
    "trips": "trip",
    "gps": "tracking_device",
    "maintenance": "maintenance",
    "documents": "document",
    "history": "audit_log",
}


@client_scoped("client_id")
class VehicleDetailView(ModulePermissionRequiredMixin, DetailView):
    model = Vehicle
    template_name = "vehicles/vehicle_detail.html"
    context_object_name = "vehicle"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "vehicle"
    permission_action = "view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        active_tab = self.request.GET.get("tab", "overview")
        context["active_tab"] = active_tab
        context["assignment_history"] = self.object.driver_assignments.select_related("driver", "assigned_by").order_by("-start_date")[:20]
        context["recent_activity"] = AuditLog.objects.filter(
            module="vehicle", entity="Vehicle", entity_id=str(self.object.pk)
        ).select_related("user").order_by("-timestamp")[:20]
        context["assign_form"] = VehicleAssignDriverForm()
        # A vehicle can change client over time; a client user must only see the
        # trips that were run for THEIR client, never a previous owner's.
        trips = scope_queryset(
            self.object.trips.select_related("client", "driver", "origin_site", "destination_site"), self.request.user
        ).order_by("-scheduled_start")
        context["trips"] = trips[:50]
        context["trip_count"] = trips.count()
        context["completed_trip_count"] = trips.filter(status="COMPLETED").count()
        context["trip_total_distance"] = trips.filter(status="COMPLETED").aggregate(
            total=Sum("actual_distance")
        )["total"] or 0

        maintenance_records = self.object.maintenance_records.order_by("-scheduled_date")
        context["maintenance_records"] = maintenance_records[:50]
        context["maintenance_due"] = maintenance_records.overdue().exists() or maintenance_records.due_today().exists()
        context["maintenance_overdue_count"] = maintenance_records.overdue().count()
        context["maintenance_cost_total"] = maintenance_records.filter(status="COMPLETED").aggregate(
            total=Sum("actual_cost")
        )["total"] or 0

        context["documents"] = get_documents_for(self.object)

        try:
            context["current_telemetry"] = self.object.current_telemetry
        except VehicleCurrentTelemetry.DoesNotExist:
            context["current_telemetry"] = None
        context["connection_status"] = connection_status_for(context["current_telemetry"])

        base_url = reverse("vehicles:vehicle_detail", kwargs={"uuid": self.object.uuid})
        context["tab_list"] = [
            {"id": "overview", "label": "Overview", "icon": "info-circle", "url": base_url},
            {"id": "driver", "label": "Driver Assignment", "icon": "person-check", "url": f"{base_url}?tab=driver"},
            {"id": "trips", "label": "Trips", "icon": "signpost-split", "url": f"{base_url}?tab=trips"},
            {"id": "gps", "label": "GPS", "icon": "geo-alt", "url": f"{base_url}?tab=gps"},
            {"id": "maintenance", "label": "Maintenance", "icon": "tools", "url": f"{base_url}?tab=maintenance"},
            {"id": "documents", "label": "Documents", "icon": "file-earmark-text", "url": f"{base_url}?tab=documents"},
            {"id": "history", "label": "History", "icon": "clock-history", "url": f"{base_url}?tab=history"},
        ]
        restrict_detail_tabs(self.request.user, context, VEHICLE_TAB_MODULES)
        return context


@require_POST
@module_permission_required("assignment", "create")
def vehicle_assign_driver(request, uuid):
    vehicle = get_object_or_404(Vehicle, uuid=uuid)
    form = VehicleAssignDriverForm(request.POST)
    if form.is_valid():
        services.assign_driver(
            vehicle=vehicle,
            driver=form.cleaned_data["driver"],
            start_date=form.cleaned_data["start_date"],
            assignment_type=form.cleaned_data["assignment_type"],
            primary_driver=form.cleaned_data["primary_driver"],
            remarks=form.cleaned_data["remarks"],
            assigned_by=request.user,
            request=request,
        )
        messages.success(request, f"{form.cleaned_data['driver'].get_full_name()} was assigned to {vehicle.registration_number}.")
    else:
        error_text = "; ".join(f"{field}: {', '.join(errs)}" for field, errs in form.errors.items())
        messages.error(request, f"Could not assign driver — {error_text}")
    return redirect("vehicles:vehicle_detail", uuid=vehicle.uuid)


@require_POST
@module_permission_required("assignment", "update")
def vehicle_unassign_driver(request, uuid, assignment_id):
    vehicle = get_object_or_404(Vehicle, uuid=uuid)
    assignment = get_object_or_404(VehicleDriverAssignment, pk=assignment_id, vehicle=vehicle)
    if assignment.status == VehicleDriverAssignment.Status.ACTIVE:
        services.end_assignment(assignment=assignment, ended_by=request.user, request=request)
        messages.success(request, f"Assignment for {assignment.driver.get_full_name()} was ended.")
    return redirect("vehicles:vehicle_detail", uuid=vehicle.uuid)


def _set_vehicle_status(request, uuid, new_status, note):
    vehicle = get_object_or_404(Vehicle, uuid=uuid)
    old_status = vehicle.status
    vehicle.status = new_status
    vehicle.updated_by = request.user
    vehicle.save(update_fields=["status", "updated_by", "updated_at"])
    log_action(
        action=AuditLog.Action.UPDATE, module="vehicle", entity="Vehicle", entity_id=str(vehicle.pk),
        old_value={"status": old_status}, new_value={"status": new_status},
        user=request.user, request=request,
    )
    messages.success(request, f"{vehicle.registration_number} {note}.")
    return redirect("vehicles:vehicle_detail", uuid=vehicle.uuid)


@require_POST
@module_permission_required("vehicle", "archive")
def vehicle_deactivate(request, uuid):
    return _set_vehicle_status(request, uuid, Vehicle.Status.INACTIVE, "has been deactivated")


@require_POST
@module_permission_required("vehicle", "archive")
def vehicle_activate(request, uuid):
    return _set_vehicle_status(request, uuid, Vehicle.Status.ACTIVE, "has been reactivated")


VEHICLE_TYPE_AUDIT_FIELDS = ["code", "name", "category_id", "is_active"]


class VehicleTypeListView(ModulePermissionRequiredMixin, ListView):
    model = VehicleType
    template_name = "vehicles/vehicle_type_list.html"
    context_object_name = "vehicle_types"
    paginate_by = 25
    permission_module = "vehicle_type"
    permission_action = "view"

    def get_queryset(self):
        qs = VehicleType.objects.select_related("category").all()

        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(Q(name__icontains=query) | Q(code__icontains=query))

        is_active = self.request.GET.get("is_active", "").strip()
        if is_active == "1":
            qs = qs.filter(is_active=True)
        elif is_active == "0":
            qs = qs.filter(is_active=False)

        category = self.request.GET.get("category", "").strip()
        if category:
            qs = qs.filter(category_id=category)

        return qs.order_by("name")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        q = self.request.GET.get("q", "")
        is_active = self.request.GET.get("is_active", "")
        category = self.request.GET.get("category", "")
        context["current_filters"] = {"q": q, "is_active": is_active, "category": category}
        context["filter_fields"] = [
            {"type": "search", "name": "q", "label": "Search", "value": q, "col": 4,
             "placeholder": "Type name or code…"},
            {"type": "select", "name": "is_active", "label": "Status", "value": is_active, "col": 2,
             "choices": [("1", "Active"), ("0", "Inactive")]},
            {"type": "select", "name": "category", "label": "Category", "value": category, "col": 3,
             "choices": [(str(c.id), c.name) for c in VehicleCategory.objects.filter(is_active=True)]},
        ]
        context["create_url"] = reverse("vehicles:vehicle_type_create")
        return context


class VehicleTypeCreateView(ModulePermissionRequiredMixin, CreateView):
    model = VehicleType
    form_class = VehicleTypeForm
    template_name = "vehicles/vehicle_type_form.html"
    permission_module = "vehicle_type"
    permission_action = "create"

    def form_valid(self, form):
        response = super().form_valid(form)
        log_action(
            action=AuditLog.Action.CREATE, module="vehicle_type", entity="VehicleType", entity_id=str(self.object.pk),
            new_value=model_to_dict_safe(self.object, VEHICLE_TYPE_AUDIT_FIELDS),
            user=self.request.user, request=self.request,
        )
        messages.success(self.request, f"Vehicle Type '{self.object.name}' was created successfully.")
        return response

    def get_success_url(self):
        return reverse("vehicles:vehicle_type_list")


class VehicleTypeUpdateView(ModulePermissionRequiredMixin, UpdateView):
    model = VehicleType
    form_class = VehicleTypeForm
    template_name = "vehicles/vehicle_type_form.html"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "vehicle_type"
    permission_action = "update"

    def form_valid(self, form):
        old_value = model_to_dict_safe(self.get_object(), VEHICLE_TYPE_AUDIT_FIELDS)
        response = super().form_valid(form)
        log_action(
            action=AuditLog.Action.UPDATE, module="vehicle_type", entity="VehicleType", entity_id=str(self.object.pk),
            old_value=old_value, new_value=model_to_dict_safe(self.object, VEHICLE_TYPE_AUDIT_FIELDS),
            user=self.request.user, request=self.request,
        )
        messages.success(self.request, f"Vehicle Type '{self.object.name}' was updated successfully.")
        return response

    def get_success_url(self):
        return reverse("vehicles:vehicle_type_list")


@require_POST
@module_permission_required("vehicle_type", "archive")
def vehicle_type_deactivate(request, uuid):
    vehicle_type = get_object_or_404(VehicleType, uuid=uuid)
    vehicle_type.is_active = False
    vehicle_type.save(update_fields=["is_active", "updated_at"])
    log_action(
        action=AuditLog.Action.UPDATE, module="vehicle_type", entity="VehicleType", entity_id=str(vehicle_type.pk),
        old_value={"is_active": True}, new_value={"is_active": False}, user=request.user, request=request,
    )
    messages.success(request, f"{vehicle_type.name} has been deactivated.")
    return redirect("vehicles:vehicle_type_list")


@require_POST
@module_permission_required("vehicle_type", "archive")
def vehicle_type_activate(request, uuid):
    vehicle_type = get_object_or_404(VehicleType, uuid=uuid)
    vehicle_type.is_active = True
    vehicle_type.save(update_fields=["is_active", "updated_at"])
    log_action(
        action=AuditLog.Action.UPDATE, module="vehicle_type", entity="VehicleType", entity_id=str(vehicle_type.pk),
        old_value={"is_active": False}, new_value={"is_active": True}, user=request.user, request=request,
    )
    messages.success(request, f"{vehicle_type.name} has been reactivated.")
    return redirect("vehicles:vehicle_type_list")


@client_scoped("vehicle__client_id")
class AssignmentListView(ModulePermissionRequiredMixin, ListView):
    """Read-only, cross-vehicle history of every vehicle-driver assignment —
    creation/ending stays exclusively on the Vehicle detail page's existing
    assign/unassign actions (apps.vehicles.services), this is a registry
    view over the same VehicleDriverAssignment rows, nothing new mutated."""

    model = VehicleDriverAssignment
    template_name = "vehicles/assignment_list.html"
    context_object_name = "assignments"
    paginate_by = 25
    permission_module = "assignment"
    permission_action = "view"

    def get_queryset(self):
        qs = VehicleDriverAssignment.objects.select_related("vehicle", "driver", "assigned_by").all()

        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(
                Q(vehicle__registration_number__icontains=query)
                | Q(driver__first_name__icontains=query)
                | Q(driver__last_name__icontains=query)
            )

        status = self.request.GET.get("status", "").strip()
        if status:
            qs = qs.filter(status=status)

        assignment_type = self.request.GET.get("assignment_type", "").strip()
        if assignment_type:
            qs = qs.filter(assignment_type=assignment_type)

        vehicle = self.request.GET.get("vehicle", "").strip()
        if vehicle:
            qs = qs.filter(vehicle_id=vehicle)

        driver = self.request.GET.get("driver", "").strip()
        if driver:
            qs = qs.filter(driver_id=driver)

        return qs.order_by("-start_date")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        q = self.request.GET.get("q", "")
        status = self.request.GET.get("status", "")
        assignment_type = self.request.GET.get("assignment_type", "")
        context["current_filters"] = {"q": q, "status": status, "assignment_type": assignment_type}
        context["filter_fields"] = [
            {"type": "search", "name": "q", "label": "Search", "value": q, "col": 4,
             "placeholder": "Vehicle registration or driver name…"},
            {"type": "select", "name": "status", "label": "Status", "value": status, "col": 2,
             "choices": VehicleDriverAssignment.Status.choices},
            {"type": "select", "name": "assignment_type", "label": "Type", "value": assignment_type, "col": 2,
             "choices": VehicleDriverAssignment.AssignmentType.choices},
        ]
        return context
