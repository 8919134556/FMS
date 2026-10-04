"""Route CRUD, list/filter/export, and ordered-stop management.

Mirrors apps.maintenance.views (same MaintenancePart formset shape, here
RouteStop) and apps.locations.views (same permission mixins, same audit
logging shape)."""

import csv

from django.contrib import messages
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.core.permissions import ModulePermissionRequiredMixin, module_permission_required
from apps.core.scoping import client_scoped, scope_queryset
from apps.core.utils import model_to_dict_safe
from apps.routes.forms import RouteForm, RouteStopFormSet
from apps.routes.models import Route

ROUTE_AUDIT_FIELDS = ["code", "name", "status", "client_id", "estimated_distance_km", "estimated_duration_minutes"]


@client_scoped("client_id", include_shared=True)
class RouteListView(ModulePermissionRequiredMixin, ListView):
    model = Route
    template_name = "routes/route_list.html"
    context_object_name = "routes"
    paginate_by = 25
    permission_module = "route"
    permission_action = "view"

    def get_queryset(self):
        qs = Route.objects.select_related("client").annotate(stop_count_annotated=Count("stops", distinct=True))

        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(Q(name__icontains=query) | Q(code__icontains=query))

        status = self.request.GET.get("status", "").strip()
        if status:
            qs = qs.filter(status=status)

        client = self.request.GET.get("client", "").strip()
        if client:
            qs = qs.filter(client_id=client)

        return qs.order_by("name")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.clients.models import Client

        q = self.request.GET.get("q", "")
        status = self.request.GET.get("status", "")
        client = self.request.GET.get("client", "")
        context["current_filters"] = {"q": q, "status": status, "client": client}
        context["filter_fields"] = [
            {"type": "search", "name": "q", "label": "Search", "value": q, "col": 4, "placeholder": "Name or code…"},
            {"type": "select", "name": "status", "label": "Status", "value": status, "col": 2, "choices": Route.Status.choices},
            {"type": "select", "name": "client", "label": "Client", "value": client, "col": 3,
             "choices": [(str(c.id), c.client_name) for c in scope_queryset(Client.objects.filter(status=Client.Status.ACTIVE), self.request.user, "id")]},
        ]
        context["export_url"] = reverse("routes:route_export")
        context["create_url"] = reverse("routes:route_create")
        return context


class RouteExportView(RouteListView):
    permission_module = "route"
    permission_action = "export"
    paginate_by = None

    def get(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="routes_export.csv"'
        writer = csv.writer(response)
        writer.writerow(["Code", "Name", "Client", "Stops", "Est. Distance (km)", "Est. Duration (min)", "Status"])
        for r in queryset:
            writer.writerow(
                [r.code, r.name, r.client.client_name if r.client_id else "", r.stop_count_annotated,
                 r.estimated_distance_km or "", r.estimated_duration_minutes or "", r.get_status_display()]
            )
        log_action(
            action=AuditLog.Action.EXPORT, module="route", entity="Route", entity_id="",
            new_value={"row_count": queryset.count()}, user=request.user, request=request,
        )
        return response


class RouteCreateView(ModulePermissionRequiredMixin, CreateView):
    model = Route
    form_class = RouteForm
    template_name = "routes/route_form.html"
    permission_module = "route"
    permission_action = "create"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.method == "POST":
            context["stops_formset"] = RouteStopFormSet(self.request.POST, instance=Route(), prefix="stops")
        else:
            context["stops_formset"] = RouteStopFormSet(instance=Route(), prefix="stops")
        return context

    def form_valid(self, form):
        route = form.save(commit=False)
        route.created_by = self.request.user
        route.updated_by = self.request.user
        route.save()

        stops_formset = RouteStopFormSet(self.request.POST, instance=route, prefix="stops")
        if not stops_formset.is_valid():
            route.delete()
            return self.render_to_response(self.get_context_data(form=form))
        stops_formset.save()

        self.object = route
        log_action(
            action=AuditLog.Action.CREATE, module="route", entity="Route", entity_id=str(route.pk),
            new_value=model_to_dict_safe(route, ROUTE_AUDIT_FIELDS),
            user=self.request.user, request=self.request,
        )
        messages.success(self.request, f"Route '{route.name}' was created successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("routes:route_detail", kwargs={"uuid": self.object.uuid})


class RouteUpdateView(ModulePermissionRequiredMixin, UpdateView):
    model = Route
    form_class = RouteForm
    template_name = "routes/route_form.html"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "route"
    permission_action = "update"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.method == "POST":
            context["stops_formset"] = RouteStopFormSet(self.request.POST, instance=self.object, prefix="stops")
        else:
            context["stops_formset"] = RouteStopFormSet(instance=self.object, prefix="stops")
        return context

    def form_valid(self, form):
        old_value = model_to_dict_safe(self.get_object(), ROUTE_AUDIT_FIELDS)
        stops_formset = RouteStopFormSet(self.request.POST, instance=self.object, prefix="stops")
        if not stops_formset.is_valid():
            return self.render_to_response(self.get_context_data(form=form))

        self.object = form.save(actor=self.request.user)
        stops_formset.instance = self.object
        stops_formset.save()

        log_action(
            action=AuditLog.Action.UPDATE, module="route", entity="Route", entity_id=str(self.object.pk),
            old_value=old_value, new_value=model_to_dict_safe(self.object, ROUTE_AUDIT_FIELDS),
            user=self.request.user, request=self.request,
        )
        messages.success(self.request, f"Route '{self.object.name}' was updated successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("routes:route_detail", kwargs={"uuid": self.object.uuid})


@client_scoped("client_id", include_shared=True)
class RouteDetailView(ModulePermissionRequiredMixin, DetailView):
    model = Route
    template_name = "routes/route_detail.html"
    context_object_name = "route"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "route"
    permission_action = "view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["stops"] = self.object.stops.select_related("site").order_by("sequence")
        route_trips = scope_queryset(self.object.trips.select_related("vehicle", "driver"), self.request.user)
        context["trips"] = route_trips.order_by("-scheduled_start")[:25]
        context["trip_count"] = route_trips.count()
        context["recent_activity"] = AuditLog.objects.filter(
            module="route", entity="Route", entity_id=str(self.object.pk)
        ).select_related("user").order_by("-timestamp")[:20]
        return context


def _set_route_status(request, uuid, new_status, note):
    route = get_object_or_404(Route, uuid=uuid)
    old_status = route.status
    route.status = new_status
    route.updated_by = request.user
    route.save(update_fields=["status", "updated_by", "updated_at"])
    log_action(
        action=AuditLog.Action.UPDATE, module="route", entity="Route", entity_id=str(route.pk),
        old_value={"status": old_status}, new_value={"status": new_status},
        user=request.user, request=request,
    )
    messages.success(request, f"{route.name} {note}.")
    return redirect("routes:route_detail", uuid=route.uuid)


@require_POST
@module_permission_required("route", "archive")
def route_deactivate(request, uuid):
    return _set_route_status(request, uuid, Route.Status.INACTIVE, "has been deactivated")


@require_POST
@module_permission_required("route", "archive")
def route_activate(request, uuid):
    return _set_route_status(request, uuid, Route.Status.ACTIVE, "has been reactivated")
