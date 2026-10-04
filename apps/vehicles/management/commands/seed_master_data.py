from django.core.management.base import BaseCommand
from django.db import transaction

from apps.vehicles.models import FuelType, VehicleCategory, VehicleType

VEHICLE_CATEGORIES = [
    ("PASSENGER", "Passenger"),
    ("LIGHT_COMMERCIAL", "Light Commercial"),
    ("HEAVY_COMMERCIAL", "Heavy Commercial"),
    ("SPECIALTY", "Specialty"),
]

# (code, name, category_code)
VEHICLE_TYPES = [
    ("SEDAN", "Sedan", "PASSENGER"),
    ("HATCHBACK", "Hatchback", "PASSENGER"),
    ("SUV", "SUV", "PASSENGER"),
    ("VAN", "Van", "LIGHT_COMMERCIAL"),
    ("TEMPO_TRAVELLER", "Tempo Traveller", "PASSENGER"),
    ("MINI_BUS", "Mini Bus", "PASSENGER"),
    ("BUS", "Bus", "PASSENGER"),
    ("LCV", "Light Commercial Vehicle", "LIGHT_COMMERCIAL"),
    ("HCV", "Heavy Commercial Vehicle", "HEAVY_COMMERCIAL"),
    ("TRUCK", "Truck", "HEAVY_COMMERCIAL"),
    ("TRAILER", "Trailer", "HEAVY_COMMERCIAL"),
    ("ELECTRIC", "Electric Vehicle", "SPECIALTY"),
]

FUEL_TYPES = [
    ("PETROL", "Petrol"),
    ("DIESEL", "Diesel"),
    ("CNG", "CNG"),
    ("LPG", "LPG"),
    ("ELECTRIC", "Electric"),
    ("HYBRID", "Hybrid"),
]


class Command(BaseCommand):
    help = "Seed vehicle master data: categories, types, and fuel types (idempotent)."

    @transaction.atomic
    def handle(self, *args, **options):
        category_count = 0
        for code, name in VEHICLE_CATEGORIES:
            _, created = VehicleCategory.objects.get_or_create(code=code, defaults={"name": name})
            category_count += int(created)

        type_count = 0
        for code, name, category_code in VEHICLE_TYPES:
            category = VehicleCategory.objects.filter(code=category_code).first()
            _, created = VehicleType.objects.get_or_create(
                code=code, defaults={"name": name, "category": category}
            )
            type_count += int(created)

        fuel_count = 0
        for code, name in FUEL_TYPES:
            _, created = FuelType.objects.get_or_create(code=code, defaults={"name": name})
            fuel_count += int(created)

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {category_count} vehicle categories, {type_count} vehicle types, "
                f"{fuel_count} fuel types (rest already existed)."
            )
        )
