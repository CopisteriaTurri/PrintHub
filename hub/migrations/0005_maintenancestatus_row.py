"""Crea la riga unica di MaintenanceStatus, così l'admin la mostra subito."""

from django.db import migrations


def create_status(apps, schema_editor):
    MaintenanceStatus = apps.get_model("hub", "MaintenanceStatus")
    MaintenanceStatus.objects.get_or_create(pk=1)


class Migration(migrations.Migration):

    dependencies = [
        ("hub", "0004_maintenancestatus"),
    ]

    operations = [
        migrations.RunPython(create_status, migrations.RunPython.noop),
    ]
