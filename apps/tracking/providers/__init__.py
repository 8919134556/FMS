"""Provider registry — the extensibility point for adding a new telematics
provider. Register a ``BaseProviderParser`` subclass here; no other code
needs to change."""

from apps.tracking.providers.generic import GenericJSONParser
from apps.tracking.providers.teltonika import TeltonikaAVLParser


class UnsupportedProviderError(Exception):
    pass


PROVIDER_PARSERS = {
    "generic": GenericJSONParser,
    "teltonika": TeltonikaAVLParser,  # Phase 3.6 — real Codec 8 AVL protocol
    # "mdvr": MdvrParser,             # reserved TrackingDevice.Provider code — not implemented yet
    # "navtelecom": NavTelecomParser, # reserved TrackingDevice.Provider code — not implemented yet
}


def get_parser(provider_code):
    parser_class = PROVIDER_PARSERS.get(provider_code)
    if parser_class is None:
        raise UnsupportedProviderError(f"No parser implemented for provider '{provider_code}'.")
    return parser_class()
