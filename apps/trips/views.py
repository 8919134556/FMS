"""Trip CRUD, list/filter/export, and the full trip lifecycle workflow
(assign -> dispatch -> start -> [delay/resume] -> complete, or cancel at
any non-terminal point).

Mirrors apps.vehicles.views / apps.clients.views (same permission mixins,
same audit logging shape) so Trips behaves like the same product. Route
maps, GPS tracking, documents, and expenses are intentionally locked tabs —
see templates/trips/trip_detail.html — pending their own modules.
"""

import csv

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.notifications import services as notification_services
from apps.clients.models import Client
from apps.core.permissions import ModulePermissionRequiredMixin, module_permission_required, restrict_detail_tabs
from apps.core.scoping import client_scoped, scope_queryset
from apps.core.utils import model_to_dict_safe
from apps.documents.services import get_documents_for
from apps.drivers.models import Driver
from apps.trips import services
from apps.trips.forms import TripAssignForm, TripCancelForm, TripCompleteForm, TripDelayForm, TripForm
from apps.trips.models import Trip
from apps.vehicles.models import Vehicle

TRIP_AUDIT_FIELDS = [
    "trip_number", "trip_type", "priority", "status", "client_id",
    "origin_site_id", "destination_site_id", "vehicle_id", "driver_id",
    "scheduled_start", "scheduled_end",
]

TRIP_SELECT_RELATED = ("client", "origin_site", "destination_site", "vehicle", "driver")

# Shared with templates so the "which actions make sense right now" list
# lives in one place instead of being re-derived with ad-hoc status checks
# in trip_list.html and trip_detail.html separately.
EDITABLE_STATUSES = [Trip.Status.DRAFT, Trip.Status.SCHEDULED, Trip.Status.ASSIGNED]
CANCELLABLE_STATUSES = [
    Trip.Status.DRAFT, Trip.Status.SCHEDULED, Trip.Status.ASSIGNED, Trip.Status.DISPATCHED,
]


class TripListView(ModulePermissionRequiredMixin, ListView):
    model = Trip
    template_name = "trips/trip_list.html"
    context_object_name = "trips"
    paginate_by = 25
    permission_module = "trip"
    permission_action = "view"

    def _filtered_queryset(self, *, apply_status, select_related=True):
        """Shared by get_queryset() (the paginated list, status included)
        and get_context_data() (the KPI/pill counts, status excluded so
        each pill shows what it would return if clicked) — one filtering
        implementation, never two."""
        qs = Trip.objects.select_related(*TRIP_SELECT_RELATED) if select_related else Trip.objects.all()
        qs = scope_queryset(qs, self.request.user)

        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(
                Q(trip_number__icontains=query)
                | Q(client__client_name__icontains=query)
                | Q(vehicle__registration_number__icontains=query)
                | Q(driver__first_name__icontains=query)
                | Q(driver__last_name__icontains=query)
                | Q(origin_site__site_name__icontains=query)
                | Q(destination_site__site_name__icontains=query)
            )

        if apply_status:
            status = self.request.GET.get("status", "").strip()
            if status:
                qs = qs.filter(status=status)

        client = self.request.GET.get("client", "").strip()
        if client:
            qs = qs.filter(client_id=client)

        vehicle = self.request.GET.get("vehicle", "").strip()
        if vehicle:
            qs = qs.filter(vehicle_id=vehicle)

        driver = self.request.GET.get("driver", "").strip()
        if driver:
            qs = qs.filter(driver_id=driver)

        origin = self.request.GET.get("origin", "").strip()
        if origin:
            qs = qs.filter(origin_site_id=origin)

        destination = self.request.GET.get("destination", "").strip()
        if destination:
            qs = qs.filter(destination_site_id=destination)

        trip_type = self.request.GET.get("trip_type", "").strip()
        if trip_type:
            qs = qs.filter(trip_type=trip_type)

        priority = self.request.GET.get("priority", "").strip()
        if priority:
            qs = qs.filter(priority=priority)

        date_from = self.request.GET.get("date_from", "").strip()
        if date_from:
            qs = qs.filter(scheduled_start__date__gte=date_from)

        date_to = self.request.GET.get("date_to", "").strip()
        if date_to:
            qs = qs.filter(scheduled_start__date__lte=date_to)

        return qs

    def get_queryset(self):
        return self._filtered_queryset(apply_status=True).order_by("-scheduled_start")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        filters = {
            name: self.request.GET.get(name, "")
            for name in [
                "q", "status", "client", "vehicle", "driver", "origin",
                "destination", "trip_type", "priority", "date_from", "date_to",
            ]
        }
        context["current_filters"] = filters
        context["filter_fields"] = [
            {"type": "search", "name": "q", "label": "Search", "value": filters["q"], "col": 3,
             "placeholder": "Trip ID, vehicle, driver, client…"},
            {"type": "select", "name": "status", "label": "Status", "value": filters["status"], "col": 2,
             "choices": Trip.Status.choices},
            {"type": "select", "name": "client", "label": "Client", "value": filters["client"], "col": 2,
             "choices": [(str(c.id), c.client_name) for c in scope_queryset(Client.objects.filter(status=Client.Status.ACTIVE), self.request.user, "id")]},
            {"type": "select", "name": "vehicle", "label": "Vehicle", "value": filters["vehicle"], "col": 2,
             "choices": [
                 (str(v.id), v.registration_number)
                 for v in scope_queryset(Vehicle.objects.filter(status=Vehicle.Status.ACTIVE), self.request.user).order_by("registration_number")
             ]},
            {"type": "select", "name": "driver", "label": "Driver", "value": filters["driver"], "col": 2,
             "choices": [
                 (str(d.id), d.get_full_name())
                 for d in scope_queryset(Driver.objects.filter(employment_status=Driver.EmploymentStatus.ACTIVE), self.request.user)
                 .order_by("first_name", "last_name")
             ]},
            {"type": "select", "name": "trip_type", "label": "Trip Type", "value": filters["trip_type"], "col": 2,
             "choices": Trip.TripType.choices},
            {"type": "select", "name": "priority", "label": "Priority", "value": filters["priority"], "col": 1,
             "choices": Trip.Priority.choices},
            {"type": "date", "name": "date_from", "label": "From", "value": filters["date_from"], "col": 1},
            {"type": "date", "name": "date_to", "label": "To", "value": filters["date_to"], "col": 1},
        ]
        querystring = self.request.GET.urlencode()
        context["export_url"] = reverse("trips:trip_export") + (f"?{querystring}" if querystring else "")
        context["create_url"] = reverse("trips:trip_create")
        context["editable_statuses"] = EDITABLE_STATUSES
        context["cancellable_statuses"] = CANCELLABLE_STATUSES
        context["now"] = timezone.now()

        # Status counts for the KPI row + quick-filter pill bar — computed
        # from every filter EXCEPT status itself (one grouped query), so
        # each number reflects "what matches my other filters right now".
        status_counts = services.trip_status_counts(self._filtered_queryset(apply_status=False, select_related=False))
        context["status_counts"] = status_counts
        context["total_trip_count"] = sum(status_counts.values())
        context["active_trip_count"] = (
            status_counts[Trip.Status.ASSIGNED] + status_counts[Trip.Status.DISPATCHED]
            + status_counts[Trip.Status.IN_PROGRESS]
        )
        # Pre-built deep-link URLs (status replaced, every other active
        # filter preserved) — same "?" + querystring shape as export_url
        # above, computed server-side since {% include ... with %} can't
        # invoke the querystring_replace tag inline.
        def _status_url(status_value):
            params = self.request.GET.copy()
            if status_value:
                params["status"] = status_value
            else:
                params.pop("status", None)
            query = params.urlencode()
            return f"{self.request.path}?{query}" if query else self.request.path

        context["kpi_urls"] = {
            "total": _status_url(""),
            "scheduled": _status_url(Trip.Status.SCHEDULED),
            "delayed": _status_url(Trip.Status.DELAYED),
            "completed": _status_url(Trip.Status.COMPLETED),
        }
        context["status_pills"] = [
            {"value": "", "label": "All", "count": sum(status_counts.values())},
            {"value": Trip.Status.SCHEDULED, "label": "Scheduled", "count": status_counts[Trip.Status.SCHEDULED]},
            {"value": Trip.Status.ASSIGNED, "label": "Assigned", "count": status_counts[Trip.Status.ASSIGNED]},
            {"value": Trip.Status.DISPATCHED, "label": "Dispatched", "count": status_counts[Trip.Status.DISPATCHED]},
            {"value": Trip.Status.IN_PROGRESS, "label": "In Progress", "count": status_counts[Trip.Status.IN_PROGRESS]},
            {"value": Trip.Status.DELAYED, "label": "Delayed", "count": status_counts[Trip.Status.DELAYED]},
            {"value": Trip.Status.COMPLETED, "label": "Completed", "count": status_counts[Trip.Status.COMPLETED]},
            {"value": Trip.Status.CANCELLED, "label": "Cancelled", "count": status_counts[Trip.Status.CANCELLED]},
        ]
        return context


class TripExportView(TripListView):
    permission_module = "trip"
    permission_action = "export"
    paginate_by = None

    def get(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="trips_export.csv"'
        writer = csv.writer(response)
        writer.writerow(
            ["Trip ID", "Client", "Origin", "Destination", "Vehicle", "Driver",
             "Scheduled Start", "Scheduled End", "Distance (km)", "Status", "Priority"]
        )
        for t in queryset:
            writer.writerow(
                [
                    t.trip_number, t.client.client_name, t.origin_site.site_name, t.destination_site.site_name,
                    t.vehicle.registration_number if t.vehicle_id else "",
                    t.driver.get_full_name() if t.driver_id else "",
                    t.scheduled_start, t.scheduled_end,
                    t.actual_distance if t.actual_distance is not None else (t.planned_distance or ""),
                    t.get_status_display(), t.get_priority_display(),
                ]
            )
        log_action(
            action=AuditLog.Action.EXPORT, module="trip", entity="Trip", entity_id="",
            new_value={"row_count": queryset.count()}, user=request.user, request=request,
        )
        return response


class TripCreateView(ModulePermissionRequiredMixin, CreateView):
    model = Trip
    form_class = TripForm
    template_name = "trips/trip_form.html"
    permission_module = "trip"
    permission_action = "create"

    def form_valid(self, form):
        target_status = Trip.Status.DRAFT if self.request.POST.get("action") == "draft" else Trip.Status.SCHEDULED
        trip = form.save(commit=False)
        trip.trip_number = services.generate_trip_number()
        trip.status = target_status
        trip.created_by = self.request.user
        trip.updated_by = self.request.user
        trip.save()
        self.object = trip
        log_action(
            action=AuditLog.Action.CREATE, module="trip", entity="Trip", entity_id=str(trip.pk),
            new_value=model_to_dict_safe(trip, TRIP_AUDIT_FIELDS),
            user=self.request.user, request=self.request,
        )
        if target_status == Trip.Status.DRAFT:
            messages.success(self.request, f"Trip {trip.trip_number} was saved as a draft.")
        else:
            messages.success(self.request, f"Trip {trip.trip_number} scheduled successfully.")
            if trip.client.account_manager_id and trip.client.account_manager_id != self.request.user.id:
                notification_services.notify(
                    trip.client.account_manager,
                    title=f"New trip scheduled for {trip.client.client_name}",
                    body=f"Trip {trip.trip_number} was scheduled by {self.request.user.get_full_name() or self.request.user.username}.",
                    link_url=reverse("trips:trip_detail", kwargs={"uuid": trip.uuid}),
                    created_by=self.request.user,
                )
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("trips:trip_detail", kwargs={"uuid": self.object.uuid})


class TripUpdateView(ModulePermissionRequiredMixin, UpdateView):
    model = Trip
    form_class = TripForm
    template_name = "trips/trip_form.html"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "trip"
    permission_action = "update"

    def form_valid(self, form):
        old_value = model_to_dict_safe(self.get_object(), TRIP_AUDIT_FIELDS)
        self.object = form.save(actor=self.request.user)
        log_action(
            action=AuditLog.Action.UPDATE, module="trip", entity="Trip", entity_id=str(self.object.pk),
            old_value=old_value, new_value=model_to_dict_safe(self.object, TRIP_AUDIT_FIELDS),
            user=self.request.user, request=self.request,
        )
        messages.success(self.request, f"Trip {self.object.trip_number} was updated successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("trips:trip_detail", kwargs={"uuid": self.object.uuid})


TRIP_TAB_MODULES = {
    "tracking": "tracking_device",
    "documents": "document",
    "activity": "audit_log",
}


@client_scoped("client_id")
class TripDetailView(ModulePermissionRequiredMixin, DetailView):
    model = Trip
    template_name = "trips/trip_detail.html"
    context_object_name = "trip"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "trip"
    permission_action = "view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        trip = self.object
        active_tab = self.request.GET.get("tab", "overview")
        context["active_tab"] = active_tab
        context["assign_form"] = TripAssignForm(trip=trip)
        context["delay_form"] = TripDelayForm()
        context["complete_form"] = TripCompleteForm()
        context["cancel_form"] = TripCancelForm()
        # Milestone events only (excludes generic field-edit UPDATE noise) —
        # powers the Overview tab's status timeline.
        context["status_timeline"] = AuditLog.objects.filter(
            module="trip", entity="Trip", entity_id=str(trip.pk)
        ).exclude(action=AuditLog.Action.UPDATE).select_related("user").order_by("timestamp")
        context["recent_activity"] = AuditLog.objects.filter(
            module="trip", entity="Trip", entity_id=str(trip.pk)
        ).select_related("user").order_by("-timestamp")[:30]
        base_url = reverse("trips:trip_detail", kwargs={"uuid": trip.uuid})
        context["tab_list"] = [
            {"id": "overview", "label": "Overview", "icon": "info-circle", "url": base_url},
            {"id": "route", "label": "Route", "icon": "signpost-split", "url": f"{base_url}?tab=route"},
            {"id": "tracking", "label": "Tracking", "icon": "broadcast", "url": f"{base_url}?tab=tracking"},
            {"id": "documents", "label": "Documents", "icon": "file-earmark-text", "url": f"{base_url}?tab=documents"},
            {"id": "expenses", "label": "Expenses", "icon": "cash-coin", "url": f"{base_url}?tab=expenses"},
            {"id": "activity", "label": "Activity", "icon": "clock-history", "url": f"{base_url}?tab=activity"},
        ]
        restrict_detail_tabs(self.request.user, context, TRIP_TAB_MODULES)
        context["editable_statuses"] = EDITABLE_STATUSES
        context["cancellable_statuses"] = CANCELLABLE_STATUSES
        context["in_progress_or_delayed"] = [Trip.Status.IN_PROGRESS, Trip.Status.DELAYED]
        context["documents"] = get_documents_for(trip)
        return context


def _redirect_to_trip(request, trip):
    """Redirects back to the trip detail page, unless the POST included a
    same-origin ``next`` (e.g. the Dispatch Board keeping the dispatcher on
    ``/dispatch/?date=...`` after an action) — trip_detail.html's own modals
    never send ``next``, so their behavior is unchanged."""
    next_url = request.POST.get("next", "")
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(next_url)
    return redirect("trips:trip_detail", uuid=trip.uuid)


@require_POST
@module_permission_required("trip", "update")
def trip_schedule(request, uuid):
    trip = get_object_or_404(Trip, uuid=uuid)
    try:
        services.schedule_trip(trip=trip, scheduled_by=request.user, request=request)
        messages.success(request, f"Trip {trip.trip_number} scheduled successfully.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return _redirect_to_trip(request, trip)


@require_POST
@module_permission_required("trip", "update")
def trip_assign(request, uuid):
    trip = get_object_or_404(Trip, uuid=uuid)
    form = TripAssignForm(request.POST, trip=trip)
    if form.is_valid():
        try:
            services.assign_trip(
                trip=trip, vehicle=form.cleaned_data["vehicle"], driver=form.cleaned_data["driver"],
                assigned_by=request.user, request=request,
            )
            messages.success(request, f"Trip {trip.trip_number} assigned successfully.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    else:
        error_text = "; ".join(f"{field}: {', '.join(errs)}" for field, errs in form.errors.items())
        messages.error(request, f"Could not assign trip — {error_text}")
    return _redirect_to_trip(request, trip)


@require_POST
@module_permission_required("trip", "update")
def trip_dispatch(request, uuid):
    trip = get_object_or_404(Trip, uuid=uuid)
    try:
        services.dispatch_trip(trip=trip, dispatched_by=request.user, request=request)
        messages.success(request, f"Trip {trip.trip_number} dispatched.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return _redirect_to_trip(request, trip)


@require_POST
@module_permission_required("trip", "update")
def trip_start(request, uuid):
    trip = get_object_or_404(Trip, uuid=uuid)
    try:
        services.start_trip(trip=trip, started_by=request.user, request=request)
        messages.success(request, f"Trip {trip.trip_number} started.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return _redirect_to_trip(request, trip)


@require_POST
@module_permission_required("trip", "update")
def trip_delay(request, uuid):
    trip = get_object_or_404(Trip, uuid=uuid)
    form = TripDelayForm(request.POST)
    if form.is_valid():
        try:
            services.delay_trip(
                trip=trip, delay_reason=form.cleaned_data["delay_reason"],
                delay_notes=form.cleaned_data["delay_notes"], delayed_by=request.user, request=request,
            )
            messages.success(request, f"Trip {trip.trip_number} marked as delayed.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    else:
        messages.error(request, "Please provide a delay reason.")
    return _redirect_to_trip(request, trip)


@require_POST
@module_permission_required("trip", "update")
def trip_resume(request, uuid):
    trip = get_object_or_404(Trip, uuid=uuid)
    try:
        services.resume_trip(trip=trip, resumed_by=request.user, request=request)
        messages.success(request, f"Trip {trip.trip_number} resumed.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return _redirect_to_trip(request, trip)


@require_POST
@module_permission_required("trip", "update")
def trip_complete(request, uuid):
    trip = get_object_or_404(Trip, uuid=uuid)
    form = TripCompleteForm(request.POST)
    if form.is_valid():
        try:
            services.complete_trip(
                trip=trip,
                actual_end=form.cleaned_data["actual_end"],
                actual_distance=form.cleaned_data["actual_distance"],
                completion_notes=form.cleaned_data["completion_notes"],
                fuel_used=form.cleaned_data["fuel_used"],
                driver_remarks=form.cleaned_data["driver_remarks"],
                completed_by=request.user, request=request,
            )
            messages.success(request, f"Trip {trip.trip_number} completed.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    else:
        error_text = "; ".join(f"{field}: {', '.join(errs)}" for field, errs in form.errors.items())
        messages.error(request, f"Could not complete trip — {error_text}")
    return _redirect_to_trip(request, trip)


@require_POST
@module_permission_required("trip", "archive")
def trip_cancel(request, uuid):
    trip = get_object_or_404(Trip, uuid=uuid)
    form = TripCancelForm(request.POST)
    if form.is_valid():
        try:
            services.cancel_trip(
                trip=trip, cancellation_reason=form.cleaned_data["cancellation_reason"],
                cancelled_by=request.user, request=request,
            )
            messages.success(request, f"Trip {trip.trip_number} was cancelled.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    else:
        messages.error(request, "A cancellation reason is required.")
    return _redirect_to_trip(request, trip)
