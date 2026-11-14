"""Eseguito in un processo terminabile dal probe HTTP."""

import uuid

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone

PROBE_DIR = ".readiness"


def remove_abandoned(storage):
    """Cancella i file `.readiness/` lasciati da un probe terminato.

    Un probe oltre PRINTHUB_READINESS_TIMEOUT viene ucciso e il suo `finally`
    non passa: il file resta. I file più vecchi del timeout (con un margine per
    gli orologi) non possono appartenere a un probe ancora vivo.
    """
    cutoff = timezone.now().timestamp() - (settings.PRINTHUB_READINESS_TIMEOUT * 2 + 60)
    try:
        _, names = storage.listdir(PROBE_DIR)
    except (OSError, NotImplementedError):
        return
    for name in names:
        path = f"{PROBE_DIR}/{name}"
        try:
            if storage.get_modified_time(path).timestamp() < cutoff:
                storage.delete(path)
        except (OSError, NotImplementedError):
            continue


class Command(BaseCommand):
    def handle(self, **options):
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        remove_abandoned(default_storage)
        name = default_storage.save(
            f"{PROBE_DIR}/{uuid.uuid4().hex}", ContentFile(b"ok")
        )
        try:
            with default_storage.open(name, "rb") as probe:
                if probe.read() != b"ok":
                    raise RuntimeError("storage")
        finally:
            default_storage.delete(name)
        layer = get_channel_layer()
        if layer is None:
            raise RuntimeError("channel layer")

        async def roundtrip():
            channel = await layer.new_channel("readiness")
            await layer.send(channel, {"type": "readiness.probe"})
            message = await layer.receive(channel)
            if message.get("type") != "readiness.probe":
                raise RuntimeError("channel layer")

        async_to_sync(roundtrip)()
