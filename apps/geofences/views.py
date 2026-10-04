"""Geofence CRUD, list/filter, and event history. Mirrors apps.locations.views
(Branch) — same permission mixins, same audit logging shape."""

import csv

from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.alerts.report import scoped_alerts
from apps.core.permissions import ModulePermissionRequiredMixin, module_permission_required
from apps.core.scoping import scope_queryset
from apps.core.utils import model_to_dict_safe
from apps.geofences.forms import GeofenceForm
from apps.geofences.models import Geofence

GEOFENCE_AUDIT_FIELDS = [
    "code", "name", "geofence_type", "speed_limit_kmh", "shape", "center_latitude", "center_longitude",
    "radius_meters", "polygon", "status", "site_id", "branch_id",
]


def _audit_value(geofence):
    value = model_to_dict_safe(geofence, GEOFENCE_AUDIT_FIELDS)
    value["vehicles"] = sorted(geofence.vehicles.values_list("registration_number", flat=True))
    return value


class _GeofenceFormMixin:
    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs


class GeofenceListView(ModulePermissionRequiredMixin, ListView):
    model = Geofence
    template_name = "geofences/geofence_list.html"
    context_object_name = "geofences"
    paginate_by = 25
    permission_module = "geofence"
    permission_action = "view"

    def get_queryset(self):
        qs = Geofence.objects.select_related("site", "branch").annotate(vehicle_count=Count("vehicles"))

        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(Q(name__icontains=query) | Q(code__icontains=query))

        status = self.request.GET.get("status", "").strip()
        if status:
            qs = qs.filter(status=status)

        geofence_type = self.request.GET.get("type", "").strip()
        if geofence_type:
            qs = qs.filter(geofence_type=geofence_type)

        return qs.order_by("name")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        q = self.request.GET.get("q", "")
        status = self.request.GET.get("status", "")
        geofence_type = self.request.GET.get("type", "")
        context["current_filters"] = {"q": q, "status": status, "type": geofence_type}
        context["filter_fields"] = [
            {"type": "search", "name": "q", "label": "Search", "value": q, "col": 4, "placeholder": "Name or code…"},
            {"type": "select", "name": "type", "label": "Type", "value": geofence_type, "col": 2,
             "choices": Geofence.GeofenceType.choices},
            {"type": "select", "name": "status", "label": "Status", "value": status, "col": 2, "choices": Geofence.Status.choices},
        ]
        context["export_url"] = reverse("geofences:geofence_export")
        context["create_url"] = reverse("geofences:geofence_create")
        return context


class GeofenceExportView(GeofenceListView):
    permission_module = "geofence"
    permission_action = "export"
    paginate_by = None

    def get(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="geofences_export.csv"'
        writer = csv.writer(response)
        writer.writerow(["Code", "Name", "Type", "Speed limit (km/h)", "Shape", "Latitude", "Longitude", "Radius (m)",
                         "Assigned vehicles", "Site", "Branch", "Status"])
        for g in queryset.prefetch_related("vehicles"):
            writer.writerow(
                [g.code, g.name, g.get_geofence_type_display(), g.speed_limit_kmh or "", g.get_shape_display(),
                 g.center_latitude, g.center_longitude, g.radius_meters,
                 " ".join(v.registration_number for v in g.vehicles.all()),
                 g.site.site_name if g.site_id else "", g.branch.name if g.branch_id else "", g.get_status_display()]
            )
        log_action(
            action=AuditLog.Action.EXPORT, module="geofence", entity="Geofence", entity_id="",
            new_value={"row_count": queryset.count()}, user=request.user, request=request,
        )
        return response


class GeofenceCreateView(_GeofenceFormMixin, ModulePermissionRequiredMixin, CreateView):
    model = Geofence
    form_class = GeofenceForm
    template_name = "geofences/geofence_form.html"
    permission_module = "geofence"
    permission_action = "create"

    def form_valid(self, form):
        self.object = form.save(actor=self.request.user)
        log_action(
            action=AuditLog.Action.CREATE, module="geofence", entity="Geofence", entity_id=str(self.object.pk),
            new_value=_audit_value(self.object),
            user=self.request.user, request=self.request,
        )
        messages.success(self.request, f"Geofence '{self.object.name}' was created successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("geofences:geofence_detail", kwargs={"uuid": self.object.uuid})


class GeofenceUpdateView(_GeofenceFormMixin, ModulePermissionRequiredMixin, UpdateView):
    model = Geofence
    form_class = GeofenceForm
    template_name = "geofences/geofence_form.html"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "geofence"
    permission_action = "update"

    def form_valid(self, form):
        old_value = _audit_value(self.get_object())
        self.object = form.save(actor=self.request.user)
        log_action(
            action=AuditLog.Action.UPDATE, module="geofence", entity="Geofence", entity_id=str(self.object.pk),
            old_value=old_value, new_value=_audit_value(self.object),
            user=self.request.user, request=self.request,
        )
        messages.success(self.request, f"Geofence '{self.object.name}' was updated successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("geofences:geofence_detail", kwargs={"uuid": self.object.uuid})


class GeofenceDetailView(ModulePermissionRequiredMixin, DetailView):
    model = Geofence
    template_name = "geofences/geofence_detail.html"
    context_object_name = "geofence"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "geofence"
    permission_action = "view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["recent_events"] = self.object.events.select_related("vehicle").order_by("-occurred_at")[:50]
        context["event_count"] = self.object.events.count()
        context["assigned_vehicles"] = scope_queryset(self.object.vehicles.all(), self.request.user).order_by(
            "registration_number")
        alerts = scoped_alerts(self.request.user).filter(
            content_type=ContentType.objects.get_for_model(Geofence), object_id=self.object.pk)
        context["alert_count"] = alerts.count()
        context["recent_alerts"] = alerts.select_related("vehicle").order_by("-occurred_at")[:20]
        context["geofence_shape_json"] = {
            "shape": self.object.shape, "polygon": self.object.polygon,
            "lat": float(self.object.center_latitude), "lon": float(self.object.center_longitude),
            "radius": self.object.radius_meters,
        }
        context["recent_activity"] = AuditLog.objects.filter(
            module="geofence", entity="Geofence", entity_id=str(self.object.pk)
        ).select_related("user").order_by("-timestamp")[:20]
        return context


def _set_geofence_status(request, uuid, new_status, note):
    geofence = get_object_or_404(Geofence, uuid=uuid)
    old_status = geofence.status
    geofence.status = new_status
    geofence.updated_by = request.user
    geofence.save(update_fields=["status", "updated_by", "updated_at"])
    log_action(
        action=AuditLog.Action.UPDATE, module="geofence", entity="Geofence", entity_id=str(geofence.pk),
        old_value={"status": old_status}, new_value={"status": new_status},
        user=request.user, request=request,
    )
    messages.success(request, f"{geofence.name} {note}.")
    return redirect("geofences:geofence_detail", uuid=geofence.uuid)


@require_POST
@module_permission_required("geofence", "archive")
def geofence_deactivate(request, uuid):
    return _set_geofence_status(request, uuid, Geofence.Status.INACTIVE, "has been deactivated")


@require_POST
@module_permission_required("geofence", "archive")
def geofence_activate(request, uuid):
    return _set_geofence_status(request, uuid, Geofence.Status.ACTIVE, "has been reactivated")
