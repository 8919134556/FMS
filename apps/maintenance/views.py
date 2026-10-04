"""Maintenance CRUD, list/filter/export, and the start -> complete (or
cancel) workflow.

Mirrors apps.trips.views (same permission mixins, same audit logging
shape, same "services layer does the validated state transition" pattern)
so Maintenance behaves like the same product. Documents stays a locked tab
— see templates/maintenance/maintenance_detail.html — pending its module.
"""

import csv
import datetime

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView, TemplateView, UpdateView

from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.core.permissions import ModulePermissionRequiredMixin, module_permission_required, restrict_detail_tabs
from apps.core.scoping import client_scoped, scope_queryset
from apps.core.utils import model_to_dict_safe
from apps.documents.services import get_documents_for
from apps.maintenance import services
from apps.maintenance.forms import MaintenanceCancelForm, MaintenanceCompleteForm, MaintenanceForm, MaintenancePartFormSet
from apps.maintenance.models import Maintenance
from apps.vehicles.models import Vehicle

MAINTENANCE_AUDIT_FIELDS = [
    "maintenance_number", "vehicle_id", "maintenance_type", "status", "priority",
    "scheduled_date", "estimated_cost", "actual_cost",
]

MAINTENANCE_SELECT_RELATED = ("vehicle", "vehicle__vehicle_type")

EDITABLE_STATUSES = [Maintenance.Status.SCHEDULED]
CANCELLABLE_STATUSES = [Maintenance.Status.SCHEDULED, Maintenance.Status.IN_PROGRESS]


@client_scoped("vehicle__client_id")
class MaintenanceListView(ModulePermissionRequiredMixin, ListView):
    model = Maintenance
    template_name = "maintenance/maintenance_list.html"
    context_object_name = "maintenance_records"
    paginate_by = 25
    permission_module = "maintenance"
    permission_action = "view"

    def get_queryset(self):
        qs = Maintenance.objects.select_related(*MAINTENANCE_SELECT_RELATED)

        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(
                Q(maintenance_number__icontains=query)
                | Q(vehicle__registration_number__icontains=query)
                | Q(vehicle__make__icontains=query)
                | Q(vehicle__model__icontains=query)
                | Q(service_center__icontains=query)
                | Q(description__icontains=query)
            )

        vehicle = self.request.GET.get("vehicle", "").strip()
        if vehicle:
            qs = qs.filter(vehicle_id=vehicle)

        maintenance_type = self.request.GET.get("maintenance_type", "").strip()
        if maintenance_type:
            qs = qs.filter(maintenance_type=maintenance_type)

        status = self.request.GET.get("status", "").strip()
        if status == "OVERDUE":
            qs = qs.overdue()
        elif status:
            qs = qs.filter(status=status)

        priority = self.request.GET.get("priority", "").strip()
        if priority:
            qs = qs.filter(priority=priority)

        service_center = self.request.GET.get("service_center", "").strip()
        if service_center:
            qs = qs.filter(service_center__icontains=service_center)

        date_from = self.request.GET.get("date_from", "").strip()
        if date_from:
            qs = qs.filter(scheduled_date__gte=date_from)

        date_to = self.request.GET.get("date_to", "").strip()
        if date_to:
            qs = qs.filter(scheduled_date__lte=date_to)

        return qs.order_by("-scheduled_date")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        filters = {
            name: self.request.GET.get(name, "")
            for name in ["q", "vehicle", "maintenance_type", "status", "priority", "service_center", "date_from", "date_to"]
        }
        context["current_filters"] = filters
        context["filter_fields"] = [
            {"type": "search", "name": "q", "label": "Search", "value": filters["q"], "col": 3,
             "placeholder": "Maintenance ID, vehicle, service center…"},
            {"type": "select", "name": "status", "label": "Status", "value": filters["status"], "col": 2,
             "choices": list(Maintenance.Status.choices) + [("OVERDUE", "Overdue")]},
            {"type": "select", "name": "maintenance_type", "label": "Type", "value": filters["maintenance_type"], "col": 2,
             "choices": Maintenance.MaintenanceType.choices},
            {"type": "select", "name": "priority", "label": "Priority", "value": filters["priority"], "col": 1,
             "choices": Maintenance.Priority.choices},
            {"type": "date", "name": "date_from", "label": "From", "value": filters["date_from"], "col": 1},
            {"type": "date", "name": "date_to", "label": "To", "value": filters["date_to"], "col": 1},
        ]
        querystring = self.request.GET.urlencode()
        context["export_url"] = reverse("maintenance:maintenance_export") + (f"?{querystring}" if querystring else "")
        context["create_url"] = reverse("maintenance:maintenance_create")
        context["editable_statuses"] = EDITABLE_STATUSES
        context["cancellable_statuses"] = CANCELLABLE_STATUSES
        return context


class MaintenanceExportView(MaintenanceListView):
    permission_module = "maintenance"
    permission_action = "export"
    paginate_by = None

    def get(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="maintenance_export.csv"'
        writer = csv.writer(response)
        writer.writerow(
            ["Maintenance ID", "Vehicle", "Type", "Scheduled Date", "Completed Date",
             "Odometer", "Status", "Priority", "Estimated Cost", "Actual Cost"]
        )
        for m in queryset:
            writer.writerow(
                [
                    m.maintenance_number, m.vehicle.registration_number, m.get_maintenance_type_display(),
                    m.scheduled_date, m.completed_date or "", m.odometer_at_service,
                    m.get_status_display(), m.get_priority_display(),
                    m.estimated_cost if m.estimated_cost is not None else "",
                    m.actual_cost if m.actual_cost is not None else "",
                ]
            )
        log_action(
            action=AuditLog.Action.EXPORT, module="maintenance", entity="Maintenance", entity_id="",
            new_value={"row_count": queryset.count()}, user=request.user, request=request,
        )
        return response


class MaintenanceCreateView(ModulePermissionRequiredMixin, CreateView):
    model = Maintenance
    form_class = MaintenanceForm
    template_name = "maintenance/maintenance_form.html"
    permission_module = "maintenance"
    permission_action = "create"

    def get_initial(self):
        initial = super().get_initial()
        vehicle_id = self.request.GET.get("vehicle", "").strip()
        if vehicle_id:
            initial["vehicle"] = vehicle_id
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.method == "POST":
            context["parts_formset"] = MaintenancePartFormSet(self.request.POST, instance=Maintenance(), prefix="parts")
        else:
            context["parts_formset"] = MaintenancePartFormSet(instance=Maintenance(), prefix="parts")
        return context

    def form_valid(self, form):
        maintenance = form.save(commit=False)
        maintenance.maintenance_number = services.generate_maintenance_number()
        maintenance.created_by = self.request.user
        maintenance.updated_by = self.request.user
        maintenance.save()

        parts_formset = MaintenancePartFormSet(self.request.POST, instance=maintenance, prefix="parts")
        if not parts_formset.is_valid():
            maintenance.delete()
            return self.render_to_response(self.get_context_data(form=form))
        parts_formset.save()

        self.object = maintenance
        log_action(
            action=AuditLog.Action.CREATE, module="maintenance", entity="Maintenance", entity_id=str(maintenance.pk),
            new_value=model_to_dict_safe(maintenance, MAINTENANCE_AUDIT_FIELDS),
            user=self.request.user, request=self.request,
        )
        messages.success(self.request, f"Maintenance {maintenance.maintenance_number} scheduled successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("maintenance:maintenance_detail", kwargs={"uuid": self.object.uuid})


class MaintenanceUpdateView(ModulePermissionRequiredMixin, UpdateView):
    model = Maintenance
    form_class = MaintenanceForm
    template_name = "maintenance/maintenance_form.html"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "maintenance"
    permission_action = "update"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.method == "POST":
            context["parts_formset"] = MaintenancePartFormSet(self.request.POST, instance=self.object, prefix="parts")
        else:
            context["parts_formset"] = MaintenancePartFormSet(instance=self.object, prefix="parts")
        return context

    def form_valid(self, form):
        old_value = model_to_dict_safe(self.get_object(), MAINTENANCE_AUDIT_FIELDS)
        parts_formset = MaintenancePartFormSet(self.request.POST, instance=self.object, prefix="parts")
        if not parts_formset.is_valid():
            return self.render_to_response(self.get_context_data(form=form))

        self.object = form.save(actor=self.request.user)
        parts_formset.instance = self.object
        parts_formset.save()

        log_action(
            action=AuditLog.Action.UPDATE, module="maintenance", entity="Maintenance", entity_id=str(self.object.pk),
            old_value=old_value, new_value=model_to_dict_safe(self.object, MAINTENANCE_AUDIT_FIELDS),
            user=self.request.user, request=self.request,
        )
        messages.success(self.request, f"Maintenance {self.object.maintenance_number} was updated successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("maintenance:maintenance_detail", kwargs={"uuid": self.object.uuid})


MAINTENANCE_TAB_MODULES = {
    "documents": "document",
    "history": "audit_log",
}


@client_scoped("vehicle__client_id")
class MaintenanceDetailView(ModulePermissionRequiredMixin, DetailView):
    model = Maintenance
    template_name = "maintenance/maintenance_detail.html"
    context_object_name = "maintenance"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "maintenance"
    permission_action = "view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        maintenance = self.object
        active_tab = self.request.GET.get("tab", "overview")
        context["active_tab"] = active_tab
        context["parts"] = maintenance.parts.all()
        context["complete_form"] = MaintenanceCompleteForm()
        context["cancel_form"] = MaintenanceCancelForm()
        context["recent_activity"] = AuditLog.objects.filter(
            module="maintenance", entity="Maintenance", entity_id=str(maintenance.pk)
        ).select_related("user").order_by("-timestamp")[:30]
        base_url = reverse("maintenance:maintenance_detail", kwargs={"uuid": maintenance.uuid})
        context["tab_list"] = [
            {"id": "overview", "label": "Overview", "icon": "info-circle", "url": base_url},
            {"id": "service", "label": "Service", "icon": "tools", "url": f"{base_url}?tab=service"},
            {"id": "parts", "label": "Parts", "icon": "box-seam", "url": f"{base_url}?tab=parts"},
            {"id": "costs", "label": "Costs", "icon": "cash-coin", "url": f"{base_url}?tab=costs"},
            {"id": "documents", "label": "Documents", "icon": "file-earmark-text", "url": f"{base_url}?tab=documents"},
            {"id": "history", "label": "History", "icon": "clock-history", "url": f"{base_url}?tab=history"},
        ]
        restrict_detail_tabs(self.request.user, context, MAINTENANCE_TAB_MODULES)
        context["editable_statuses"] = EDITABLE_STATUSES
        context["cancellable_statuses"] = CANCELLABLE_STATUSES
        context["documents"] = get_documents_for(maintenance)
        return context


def _redirect_to_maintenance(maintenance):
    return redirect("maintenance:maintenance_detail", uuid=maintenance.uuid)


@require_POST
@module_permission_required("maintenance", "update")
def maintenance_start(request, uuid):
    maintenance = get_object_or_404(Maintenance, uuid=uuid)
    try:
        services.start_maintenance(maintenance=maintenance, started_by=request.user, request=request)
        messages.success(request, f"Maintenance {maintenance.maintenance_number} started.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return _redirect_to_maintenance(maintenance)


@require_POST
@module_permission_required("maintenance", "update")
def maintenance_complete(request, uuid):
    maintenance = get_object_or_404(Maintenance, uuid=uuid)
    form = MaintenanceCompleteForm(request.POST)
    if form.is_valid():
        try:
            services.complete_maintenance(
                maintenance=maintenance,
                completed_date=form.cleaned_data["completed_date"],
                final_odometer=form.cleaned_data["final_odometer"],
                actual_cost=form.cleaned_data["actual_cost"],
                labor_cost=form.cleaned_data["labor_cost"],
                work_performed=form.cleaned_data["work_performed"],
                completion_notes=form.cleaned_data["completion_notes"],
                next_service_date=form.cleaned_data["next_service_date"],
                next_service_odometer=form.cleaned_data["next_service_odometer"],
                completed_by=request.user, request=request,
            )
            messages.success(request, f"Maintenance {maintenance.maintenance_number} completed.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    else:
        error_text = "; ".join(f"{field}: {', '.join(errs)}" for field, errs in form.errors.items())
        messages.error(request, f"Could not complete maintenance — {error_text}")
    return _redirect_to_maintenance(maintenance)


@require_POST
@module_permission_required("maintenance", "archive")
def maintenance_cancel(request, uuid):
    maintenance = get_object_or_404(Maintenance, uuid=uuid)
    form = MaintenanceCancelForm(request.POST)
    if form.is_valid():
        try:
            services.cancel_maintenance(
                maintenance=maintenance, cancellation_reason=form.cleaned_data["cancellation_reason"],
                cancelled_by=request.user, request=request,
            )
            messages.success(request, f"Maintenance {maintenance.maintenance_number} was cancelled.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    else:
        messages.error(request, "A cancellation reason is required.")
    return _redirect_to_maintenance(maintenance)


class ServiceScheduleView(ModulePermissionRequiredMixin, TemplateView):
    """A date-bucketed view over the same Maintenance records the main list
    already manages — Overdue / Due Today / Due This Week / Upcoming — using
    the model manager's existing overdue()/due_today()/due_soon() rules
    (apps.maintenance.models.MaintenanceQuerySet) so this page can never
    disagree with the dashboard or the list's own OVERDUE filter about what
    "overdue" means. No new model, no new mutation — Start/Complete/Cancel
    stay on the existing maintenance detail page."""

    template_name = "maintenance/service_schedule.html"
    permission_module = "maintenance"
    permission_action = "view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        vehicle = self.request.GET.get("vehicle", "").strip()
        priority = self.request.GET.get("priority", "").strip()

        base_qs = scope_queryset(
            Maintenance.objects.select_related(*MAINTENANCE_SELECT_RELATED), self.request.user, "vehicle__client_id"
        )
        if vehicle:
            base_qs = base_qs.filter(vehicle_id=vehicle)
        if priority:
            base_qs = base_qs.filter(priority=priority)

        overdue_ids = base_qs.overdue().values_list("id", flat=True)
        due_today_ids = base_qs.due_today().values_list("id", flat=True)
        due_soon_ids = base_qs.due_soon().values_list("id", flat=True)

        context["overdue"] = base_qs.filter(id__in=overdue_ids).order_by("scheduled_date")
        context["due_today"] = base_qs.filter(id__in=due_today_ids).order_by("scheduled_date")
        context["due_this_week"] = base_qs.filter(id__in=due_soon_ids).order_by("scheduled_date")
        context["upcoming"] = base_qs.filter(
            status=Maintenance.Status.SCHEDULED,
            scheduled_date__gt=timezone.now().date() + datetime.timedelta(days=Maintenance.DUE_SOON_WINDOW_DAYS),
        ).order_by("scheduled_date")[:25]

        context["due_soon_window"] = Maintenance.DUE_SOON_WINDOW_DAYS
        context["current_filters"] = {"vehicle": vehicle, "priority": priority}
        context["filter_fields"] = [
            {"type": "select", "name": "vehicle", "label": "Vehicle", "value": vehicle, "col": 3,
             "choices": [
                 (str(v.id), v.registration_number)
                 for v in scope_queryset(Vehicle.objects.filter(status=Vehicle.Status.ACTIVE), self.request.user).order_by("registration_number")
             ]},
            {"type": "select", "name": "priority", "label": "Priority", "value": priority, "col": 2,
             "choices": Maintenance.Priority.choices},
        ]
        return context
