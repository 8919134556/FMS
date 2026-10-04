"""GPS Device management UI — mounted at ``/tracking/`` (apps/tracking/urls.py).

Mirrors apps.maintenance/apps.vehicles: CBVs for CRUD + list, function-based
views for the workflow-style actions (assign/unassign/disable/enable/
regenerate key), all delegating state changes to apps.tracking.services.
"""

import uuid

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView, TemplateView, UpdateView

from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.core.models import SystemSettings
from apps.core.permissions import ModulePermissionRequiredMixin, has_admin_access, module_permission_required, user_has_permission
from apps.core.scoping import scope_queryset
from apps.core.utils import model_to_dict_safe
from apps.tracking import command_services, location_report, services, trip_report
from apps.tracking.forms import DeviceCommandForm, TrackingDeviceAssignForm, TrackingDeviceForm
from apps.tracking.models import DeviceCommand, TrackingDevice, VehicleCurrentTelemetry
from apps.tracking.services import connection_status_for
from apps.tracking.telematics_service.command_encoding import SUPPORTED_COMMAND_TYPES
from apps.vehicles.models import Vehicle

DEVICE_AUDIT_FIELDS = ["imei", "name", "provider", "status", "vehicle_id"]
DEVICE_KEY_SESSION_KEY = "tracking_device_raw_key"


class DeviceListView(ModulePermissionRequiredMixin, ListView):
    model = TrackingDevice
    template_name = "tracking/device_list.html"
    context_object_name = "devices"
    paginate_by = 25
    permission_module = "tracking_device"
    permission_action = "view"

    def get_queryset(self):
        qs = scope_queryset(TrackingDevice.objects.select_related("vehicle"), self.request.user, "vehicle__client_id")

        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(imei__icontains=query) | qs.filter(name__icontains=query)

        provider = self.request.GET.get("provider", "").strip()
        if provider:
            qs = qs.filter(provider=provider)

        status = self.request.GET.get("status", "").strip()
        if status:
            qs = qs.filter(status=status)

        assigned = self.request.GET.get("assigned", "").strip()
        if assigned == "yes":
            qs = qs.filter(vehicle__isnull=False)
        elif assigned == "no":
            qs = qs.filter(vehicle__isnull=True)

        return qs.order_by("-created_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        filters = {
            name: self.request.GET.get(name, "") for name in ["q", "provider", "status", "assigned"]
        }
        context["filter_fields"] = [
            {"type": "search", "name": "q", "label": "Search", "value": filters["q"], "col": 3,
             "placeholder": "IMEI or device name…"},
            {"type": "select", "name": "provider", "label": "Provider", "value": filters["provider"], "col": 2,
             "choices": TrackingDevice.Provider.choices},
            {"type": "select", "name": "status", "label": "Status", "value": filters["status"], "col": 2,
             "choices": TrackingDevice.Status.choices},
            {"type": "select", "name": "assigned", "label": "Assignment", "value": filters["assigned"], "col": 2,
             "choices": [("yes", "Assigned"), ("no", "Unassigned")]},
        ]
        context["create_url"] = reverse("tracking:device_create")
        return context


class DeviceCreateView(ModulePermissionRequiredMixin, CreateView):
    model = TrackingDevice
    form_class = TrackingDeviceForm
    template_name = "tracking/device_form.html"
    permission_module = "tracking_device"
    permission_action = "create"

    def form_valid(self, form):
        self.object = form.save(actor=self.request.user)
        log_action(
            action=AuditLog.Action.CREATE, module="tracking_device", entity="TrackingDevice",
            entity_id=str(self.object.pk), new_value=model_to_dict_safe(self.object, DEVICE_AUDIT_FIELDS),
            user=self.request.user, request=self.request,
        )
        raw_key = services.issue_device_key(device=self.object, actor=self.request.user, request=self.request)
        self.request.session[DEVICE_KEY_SESSION_KEY] = raw_key
        messages.success(self.request, f"Device '{self.object.imei}' was created successfully.")
        return redirect("tracking:device_key_reveal", uuid=self.object.uuid)


class DeviceUpdateView(ModulePermissionRequiredMixin, UpdateView):
    model = TrackingDevice
    form_class = TrackingDeviceForm
    template_name = "tracking/device_form.html"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "tracking_device"
    permission_action = "update"

    def form_valid(self, form):
        old_value = model_to_dict_safe(self.get_object(), DEVICE_AUDIT_FIELDS)
        self.object = form.save(actor=self.request.user)
        log_action(
            action=AuditLog.Action.UPDATE, module="tracking_device", entity="TrackingDevice",
            entity_id=str(self.object.pk), old_value=old_value,
            new_value=model_to_dict_safe(self.object, DEVICE_AUDIT_FIELDS),
            user=self.request.user, request=self.request,
        )
        messages.success(self.request, f"Device '{self.object.imei}' was updated successfully.")
        return redirect("tracking:device_detail", uuid=self.object.uuid)


class DeviceDetailView(ModulePermissionRequiredMixin, DetailView):
    model = TrackingDevice
    template_name = "tracking/device_detail.html"
    context_object_name = "device"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "tracking_device"
    permission_action = "view"

    def get_queryset(self):
        return scope_queryset(TrackingDevice.objects.select_related("vehicle"), self.request.user, "vehicle__client_id")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        device = self.object
        current = None
        if device.vehicle_id:
            try:
                current = device.vehicle.current_telemetry
            except VehicleCurrentTelemetry.DoesNotExist:
                current = None
        context["current_telemetry"] = current
        context["connection_status"] = connection_status_for(current)
        context["recent_raw_events"] = scope_queryset(device.raw_events.all(), self.request.user)[:20]
        context["assign_form"] = TrackingDeviceAssignForm()
        context["recent_activity"] = AuditLog.objects.filter(
            module="tracking_device", entity="TrackingDevice", entity_id=str(device.pk)
        ).select_related("user").order_by("-timestamp")[:20]
        context["recent_commands"] = device.commands.all()[:10]
        context["can_command"] = user_has_permission(self.request.user, "tracking_device", "command")
        context["supported_commands"] = sorted(
            (dict(DeviceCommand.CommandType.choices)[c] for c in SUPPORTED_COMMAND_TYPES.get(device.provider, set())),
        )
        return context


@require_POST
@module_permission_required("tracking_device", "update")
def device_assign(request, uuid):
    device = get_object_or_404(TrackingDevice, uuid=uuid)
    form = TrackingDeviceAssignForm(request.POST)
    if form.is_valid():
        try:
            services.assign_device(device=device, vehicle=form.cleaned_data["vehicle"], actor=request.user, request=request)
            messages.success(request, f"Device '{device.imei}' assigned to {form.cleaned_data['vehicle'].registration_number}.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    else:
        messages.error(request, "Please choose a vehicle to assign.")
    return redirect("tracking:device_detail", uuid=device.uuid)


@require_POST
@module_permission_required("tracking_device", "update")
def device_unassign(request, uuid):
    device = get_object_or_404(TrackingDevice, uuid=uuid)
    if device.vehicle_id:
        services.unassign_device(device=device, actor=request.user, request=request)
        messages.success(request, f"Device '{device.imei}' was unassigned.")
    return redirect("tracking:device_detail", uuid=device.uuid)


@require_POST
@module_permission_required("tracking_device", "archive")
def device_disable(request, uuid):
    device = get_object_or_404(TrackingDevice, uuid=uuid)
    services.disable_device(device=device, actor=request.user, request=request)
    messages.success(request, f"Device '{device.imei}' has been disabled.")
    return redirect("tracking:device_detail", uuid=device.uuid)


@require_POST
@module_permission_required("tracking_device", "archive")
def device_enable(request, uuid):
    device = get_object_or_404(TrackingDevice, uuid=uuid)
    services.enable_device(device=device, actor=request.user, request=request)
    messages.success(request, f"Device '{device.imei}' has been re-activated.")
    return redirect("tracking:device_detail", uuid=device.uuid)


@require_POST
@module_permission_required("tracking_device", "update")
def device_regenerate_key(request, uuid):
    device = get_object_or_404(TrackingDevice, uuid=uuid)
    raw_key = services.issue_device_key(device=device, actor=request.user, request=request)
    request.session[DEVICE_KEY_SESSION_KEY] = raw_key
    return redirect("tracking:device_key_reveal", uuid=device.uuid)


@module_permission_required("tracking_device", "update")
def device_key_reveal(request, uuid):
    """One-time reveal of a freshly issued/regenerated device secret — never
    retrievable again after this page is left (the raw value is popped from
    the session, not stored anywhere)."""
    device = get_object_or_404(TrackingDevice, uuid=uuid)
    raw_key = request.session.pop(DEVICE_KEY_SESSION_KEY, None)
    if raw_key is None:
        messages.error(request, "This device key has already been viewed. Regenerate to issue a new one.")
        return redirect("tracking:device_detail", uuid=device.uuid)
    return render(request, "tracking/device_key_reveal.html", {"device": device, "raw_key": raw_key})


class LiveTrackingView(ModulePermissionRequiredMixin, TemplateView):
    """The Live Fleet Map (Phase 3.4) — a pure read/UI page. Deliberately
    renders NO telemetry data server-side; the template embeds only a
    vehicle uuid / status hint (never a coordinate) and the JS fetches
    everything else from the already-RBAC'd fleet API."""

    template_name = "tracking/live_map.html"
    permission_module = "tracking_device"
    permission_action = "view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        raw_vehicle = self.request.GET.get("vehicle", "").strip()
        initial_vehicle_uuid = ""
        if raw_vehicle:
            try:
                uuid.UUID(raw_vehicle)
                if scope_queryset(Vehicle.objects.all(), self.request.user).filter(uuid=raw_vehicle).exists():
                    initial_vehicle_uuid = raw_vehicle
            except ValueError:
                pass  # garbage value silently dropped, never a 404/500

        status = self.request.GET.get("status", "").strip().lower()
        context["initial_vehicle_uuid"] = initial_vehicle_uuid
        context["initial_status"] = status if status in {"online", "offline", "stale", "moving", "idle"} else ""
        # Non-sensitive count (not coordinates) — reuses the same service the
        # dashboard widget uses, so the two numbers can never drift apart.
        context["no_telemetry_count"] = services.fleet_connectivity_counts(self.request.user)["no_telemetry"]
        # comms reports track time in the Settings timezone, so display it in that
        # same zone regardless of the viewer's browser/OS clock.
        context["display_timezone"] = SystemSettings.load().default_timezone or "UTC"
        # Admins get the exact command to restart the data feed; everyone else is told to ask.
        context["can_manage_feed"] = has_admin_access(self.request.user)
        return context


class TripReportView(ModulePermissionRequiredMixin, TemplateView):
    """Ignition-derived Trip Report (apps.tracking.trip_report) — same
    no-server-side-telemetry approach as LiveTrackingView: the shell renders
    with no trip data, and the JS fetches everything from the RBAC'd API."""

    template_name = "tracking/trip_report.html"
    permission_module = "tracking_device"
    permission_action = "view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["display_timezone"] = SystemSettings.load().default_timezone or "UTC"
        context["max_custom_range_days"] = trip_report.MAX_CUSTOM_RANGE_DAYS
        # Non-sensitive identity only (uuid + plate) — populates the vehicle filter
        # without the page ever rendering a position server-side.
        context["vehicles"] = list(
            scope_queryset(Vehicle.objects.filter(tracking_device__isnull=False), self.request.user)
            .order_by("registration_number")
            .values("uuid", "registration_number")
        )
        return context


class OdometerReportView(TripReportView):
    """Odometer Report (apps.tracking.odometer_report) — the Trip Report's
    shell, RBAC and vehicle-filter context, its own template; all data comes
    from the RBAC'd API (OdometerReportDataView)."""

    template_name = "tracking/odometer_report.html"


class LocationReportView(TripReportView):
    """Location Data Report (apps.tracking.location_report) — same shell,
    RBAC and scoped vehicle list as the Trip Report; records, analysis and
    exports come from LocationReportDataView / LocationReportExportView."""

    template_name = "tracking/location_report.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_sizes"] = location_report.PAGE_SIZES
        return context


# ---------------------------------------------------------------------------
# Device Command management (Phase 3.5) — list/detail require the standard
# tracking_device/view permission (same as device management); creating,
# cancelling, and retrying a command require the dedicated tracking_device/
# command action, so a role that can merely view/manage devices cannot send
# commands without an explicit grant.
# ---------------------------------------------------------------------------

class CommandListView(ModulePermissionRequiredMixin, ListView):
    model = DeviceCommand
    template_name = "tracking/command_list.html"
    context_object_name = "commands"
    paginate_by = 25
    permission_module = "tracking_device"
    permission_action = "view"

    def get_queryset(self):
        qs = scope_queryset(
            DeviceCommand.objects.select_related("device", "created_by"), self.request.user, "device__vehicle__client_id"
        )

        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(device__imei__icontains=query)

        status = self.request.GET.get("status", "").strip()
        if status:
            qs = qs.filter(status=status)

        command_type = self.request.GET.get("command_type", "").strip()
        if command_type:
            qs = qs.filter(command_type=command_type)

        device = self.request.GET.get("device", "").strip()
        if device:
            qs = qs.filter(device__uuid=device)

        return qs.order_by("-created_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        filters = {name: self.request.GET.get(name, "") for name in ["q", "status", "command_type", "device"]}
        context["filter_fields"] = [
            {"type": "search", "name": "q", "label": "Search", "value": filters["q"], "col": 3,
             "placeholder": "Device IMEI…"},
            {"type": "select", "name": "status", "label": "Status", "value": filters["status"], "col": 2,
             "choices": DeviceCommand.Status.choices},
            {"type": "select", "name": "command_type", "label": "Type", "value": filters["command_type"], "col": 2,
             "choices": DeviceCommand.CommandType.choices},
        ]
        context["can_command"] = user_has_permission(self.request.user, "tracking_device", "command")
        context["create_url"] = reverse("tracking:command_create")
        return context


class CommandCreateView(ModulePermissionRequiredMixin, CreateView):
    model = DeviceCommand
    form_class = DeviceCommandForm
    template_name = "tracking/command_form.html"
    permission_module = "tracking_device"
    permission_action = "command"

    def get_initial(self):
        initial = super().get_initial()
        device_uuid = self.request.GET.get("device", "").strip()
        if device_uuid:
            initial["device"] = TrackingDevice.objects.filter(uuid=device_uuid).first()
        return initial

    def form_valid(self, form):
        self.object = command_services.create_command(
            device=form.cleaned_data["device"],
            command_type=form.cleaned_data["command_type"],
            payload=form.cleaned_data["payload"],
            expires_at=form.cleaned_data.get("expires_at"),
            actor=self.request.user,
            request=self.request,
        )
        messages.success(self.request, f"Command '{self.object.get_command_type_display()}' queued for {self.object.device.imei}.")
        return redirect("tracking:command_detail", uuid=self.object.uuid)


class CommandDetailView(ModulePermissionRequiredMixin, DetailView):
    model = DeviceCommand
    template_name = "tracking/command_detail.html"
    context_object_name = "command"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "tracking_device"
    permission_action = "view"

    def get_queryset(self):
        return scope_queryset(
            DeviceCommand.objects.select_related("device", "created_by"), self.request.user, "device__vehicle__client_id"
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        command = self.object
        can_command = user_has_permission(self.request.user, "tracking_device", "command")
        context["can_cancel"] = can_command and command.status in (
            DeviceCommand.Status.PENDING, DeviceCommand.Status.QUEUED,
        )
        context["can_retry"] = can_command and command.status in (
            DeviceCommand.Status.FAILED, DeviceCommand.Status.EXPIRED,
        ) and command.retry_count < command.max_retries
        return context


@require_POST
@module_permission_required("tracking_device", "command")
def command_cancel(request, uuid):
    command = get_object_or_404(DeviceCommand, uuid=uuid)
    try:
        command_services.cancel_command(command=command, actor=request.user, request=request)
        messages.success(request, "Command cancelled.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return redirect("tracking:command_detail", uuid=command.uuid)


@require_POST
@module_permission_required("tracking_device", "command")
def command_retry(request, uuid):
    command = get_object_or_404(DeviceCommand, uuid=uuid)
    try:
        command_services.retry_command(command=command, actor=request.user, request=request)
        messages.success(request, "Command re-queued for delivery.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return redirect("tracking:command_detail", uuid=command.uuid)
