"""Eseguito in un processo terminabile dal probe HTTP."""

import uuid

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    def handle(self, **options):
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        name = default_storage.save(
            f".readiness/{uuid.uuid4().hex}", ContentFile(b"ok")
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
