"""Per-provider DeviceCommand -> wire-bytes encoding.

Only commands a provider's real protocol genuinely supports are encodable
here — everything else raises ``UnsupportedCommandError`` rather than
pretending to encode something the device could never understand. This is
also the single source of truth for which ``DeviceCommand.CommandType``
values the UI may offer for a given device (see
``apps.tracking.forms.DeviceCommandForm`` and ``DeviceDetailView``).
"""

import json

from apps.tracking.models import DeviceCommand
from apps.tracking.telematics_service.protocol import teltonika as teltonika_protocol

CommandType = DeviceCommand.CommandType


class UnsupportedCommandError(Exception):
    """Raised when a command type has no real encoding for a given
    provider — the caller must fail the command clearly, never send
    something the device can't understand and never retry it."""


# Real, documented Teltonika Codec 12 text commands. SET_REPORTING_INTERVAL
# is deliberately absent: real interval configuration on Teltonika devices
# is done via device-model-specific numbered SMS/GPRS parameters, not a
# plain text command — encoding it here would mean inventing a parameter ID
# with no verified specification behind it.
_TELTONIKA_COMMAND_TEXT = {
    CommandType.PING: "getinfo",
    CommandType.REQUEST_LOCATION: "getgps",
    CommandType.REBOOT: "reboot",
}

# The command types genuinely offerable per provider — consumed by the
# claim/dispatch failure path below AND by DeviceCommandForm's capability
# validation, so the UI and the wire encoder can never disagree.
SUPPORTED_COMMAND_TYPES = {
    "generic": {
        CommandType.PING, CommandType.REQUEST_LOCATION, CommandType.REBOOT,
        CommandType.SET_REPORTING_INTERVAL, CommandType.CUSTOM,
    },
    "teltonika": {CommandType.PING, CommandType.REQUEST_LOCATION, CommandType.REBOOT, CommandType.CUSTOM},
}


def _encode_generic(command) -> bytes:
    message = {"type": "command", "id": str(command.uuid), "command_type": command.command_type, "payload": command.payload}
    return json.dumps(message).encode("utf-8") + b"\n"


def _encode_teltonika(command) -> bytes:
    if command.command_type == CommandType.CUSTOM:
        text = (command.payload or {}).get("command_text")
        if not text:
            raise UnsupportedCommandError("CUSTOM commands require a 'command_text' payload for this provider.")
    else:
        text = _TELTONIKA_COMMAND_TEXT.get(command.command_type)
        if text is None:
            raise UnsupportedCommandError(
                f"{command.get_command_type_display()} is not supported for the Teltonika protocol."
            )
    return teltonika_protocol.encode_command(text)


_ENCODERS = {
    "generic": _encode_generic,
    "teltonika": _encode_teltonika,
}


def encode_command_for_provider(provider: str, command) -> bytes:
    """Raises UnsupportedCommandError for a provider with no encoder at
    all, or a command type that provider's encoder can't build."""
    encoder = _ENCODERS.get(provider)
    if encoder is None:
        raise UnsupportedCommandError(f"No command encoder registered for provider '{provider}'.")
    return encoder(command)
