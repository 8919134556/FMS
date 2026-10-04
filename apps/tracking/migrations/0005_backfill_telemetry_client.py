from django.db import migrations
from django.db.models import OuterRef, Subquery


def backfill_client(apps, schema_editor):
    """Existing history/raw rows predate client attribution. Best effort: use
    the vehicle's *current* client (the only ownership we know for old rows).
    Rows with no vehicle stay NULL (staff-only). One UPDATE per table."""
    Vehicle = apps.get_model("vehicles", "Vehicle")
    TelemetryEvent = apps.get_model("tracking", "TelemetryEvent")
    owner = Vehicle.objects.filter(pk=OuterRef("vehicle_id")).values("client_id")[:1]
    TelemetryEvent.objects.filter(client__isnull=True, vehicle__isnull=False).update(client_id=Subquery(owner))


class Migration(migrations.Migration):
    dependencies = [("tracking", "0004_rawtelemetryevent_client_rawtelemetryevent_vehicle_and_more")]
    operations = [migrations.RunPython(backfill_client, migrations.RunPython.noop)]
