import json

from django import forms

from apps.core.forms import BootstrapFormMixin
from apps.tracking.models import DeviceCommand, TrackingDevice
from apps.tracking.telematics_service.command_encoding import SUPPORTED_COMMAND_TYPES
from apps.vehicles.models import Vehicle

DEVICE_FIELDS = [
    "name", "imei", "serial_number", "device_model", "manufacturer",
    "sim_number", "sim_provider", "provider", "firmware_version",
]
OPTIONAL_DEVICE_FIELDS = [
    "name", "serial_number", "device_model", "manufacturer", "sim_number", "sim_provider", "firmware_version",
]


class TrackingDeviceForm(BootstrapFormMixin, forms.ModelForm):
    """Create/edit device master data only — vehicle assignment and status
    (active/disabled) are handled by dedicated actions
    (assign/unassign/disable/enable), not this form, so there's exactly one
    mutation path for each."""

    class Meta:
        model = TrackingDevice
        fields = DEVICE_FIELDS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in OPTIONAL_DEVICE_FIELDS:
            self.fields[name].required = False

    def save(self, commit=True, actor=None):
        device = super().save(commit=False)
        if not device.pk:
            device.created_by = actor
        device.updated_by = actor
        if commit:
            device.save()
        return device


class TrackingDeviceAssignForm(BootstrapFormMixin, forms.Form):
    vehicle = forms.ModelChoiceField(queryset=Vehicle.objects.none(), label="Vehicle")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vehicle"].queryset = Vehicle.objects.filter(tracking_device__isnull=True).order_by(
            "registration_number"
        )


class DeviceCommandForm(BootstrapFormMixin, forms.ModelForm):
    """Create form for DeviceCommand. Deliberately no raw freeform socket
    payload box for ordinary users — only CUSTOM exposes a JSON textarea
    (still validated as a JSON object); every other command type gets a
    type-appropriate field (or none at all) that's assembled into
    ``payload`` in ``clean()``. The view calls
    ``apps.tracking.command_services.create_command`` directly with
    ``form.cleaned_data`` rather than ``form.save()``, keeping DB mutation
    + audit logging in exactly one place."""

    interval_seconds = forms.IntegerField(
        required=False, min_value=5, max_value=86400,
        label="Reporting Interval (seconds)",
        help_text="Required for 'Set Reporting Interval' commands.",
    )
    custom_payload = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 3}),
        label="Payload (JSON)",
        help_text="Required for 'Custom' commands — must be a valid JSON object.",
    )

    class Meta:
        model = DeviceCommand
        fields = ["device", "command_type", "expires_at"]
        widgets = {
            "expires_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["device"].queryset = TrackingDevice.objects.filter(
            status=TrackingDevice.Status.ACTIVE
        ).order_by("imei")
        self.fields["expires_at"].required = False
        self.fields["expires_at"].input_formats = ["%Y-%m-%dT%H:%M"]

    def clean(self):
        cleaned = super().clean()
        command_type = cleaned.get("command_type")
        device = cleaned.get("device")
        payload = {}

        # Capability validation — the UI must never let an operator queue a
        # command their device's actual protocol can't encode (see
        # apps.tracking.telematics_service.command_encoding, the single
        # source of truth both this form and the dispatch loop consult).
        if device and command_type:
            supported = SUPPORTED_COMMAND_TYPES.get(device.provider, set())
            if command_type not in supported:
                self.add_error(
                    "command_type",
                    f"{dict(DeviceCommand.CommandType.choices).get(command_type, command_type)} is not supported "
                    f"for the {device.get_provider_display()} protocol.",
                )

        if command_type == DeviceCommand.CommandType.SET_REPORTING_INTERVAL:
            interval = cleaned.get("interval_seconds")
            if not interval:
                self.add_error("interval_seconds", "This field is required for Set Reporting Interval commands.")
            else:
                payload = {"interval_seconds": interval}
        elif command_type == DeviceCommand.CommandType.CUSTOM:
            raw = (cleaned.get("custom_payload") or "").strip()
            if not raw:
                self.add_error("custom_payload", "This field is required for Custom commands.")
            elif device and device.provider == TrackingDevice.Provider.TELTONIKA:
                # Teltonika's Codec 12 command channel takes a raw text
                # command (e.g. "getver"), not a JSON object.
                payload = {"command_text": raw}
            else:
                try:
                    parsed = json.loads(raw)
                    if not isinstance(parsed, dict):
                        raise ValueError("Payload must be a JSON object.")
                    payload = parsed
                except (json.JSONDecodeError, ValueError):
                    self.add_error("custom_payload", "Must be a valid JSON object.")
        cleaned["payload"] = payload
        return cleaned
