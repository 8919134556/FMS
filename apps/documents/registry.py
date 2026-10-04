"""Central list of every model a Document can attach to.

One place to add a new documentable entity in the future, instead of
touching the form, the filter dropdown, and every template separately.
"""

from apps.clients.models import Client
from apps.drivers.models import Driver
from apps.locations.models import Site
from apps.maintenance.models import Maintenance
from apps.trips.models import Trip
from apps.vehicles.models import Vehicle

DOCUMENTABLE_MODELS = {
    "client": {"model": Client, "label": "Client", "detail_url": "clients:client_detail"},
    "site": {"model": Site, "label": "Site", "detail_url": "locations:site_detail"},
    "vehicle": {"model": Vehicle, "label": "Vehicle", "detail_url": "vehicles:vehicle_detail"},
    "driver": {"model": Driver, "label": "Driver", "detail_url": "drivers:driver_detail"},
    "trip": {"model": Trip, "label": "Trip", "detail_url": "trips:trip_detail"},
    "maintenance": {"model": Maintenance, "label": "Maintenance", "detail_url": "maintenance:maintenance_detail"},
}

ENTITY_TYPE_CHOICES = [(key, value["label"]) for key, value in DOCUMENTABLE_MODELS.items()]


def get_entity_config(entity_type):
    return DOCUMENTABLE_MODELS.get(entity_type)
