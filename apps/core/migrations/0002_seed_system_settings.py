from django.db import migrations


def create_default_settings(apps, schema_editor):
    """Pre-creates the singleton row so ``SystemSettings.load()`` (called on
    every page via the ``site_meta`` context processor) is always a plain
    SELECT — never a lazy get_or_create INSERT on whichever request happens
    to run first."""
    SystemSettings = apps.get_model("core", "SystemSettings")
    SystemSettings.objects.get_or_create(pk=1)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_default_settings, noop_reverse),
    ]
