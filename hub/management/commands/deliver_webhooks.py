import time

from django.core.management.base import BaseCommand

from hub.webhooks import deliver_once


class Command(BaseCommand):
    help = "Consegna webhook dalla outbox; eseguire sotto supervisione."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, once, **options):
        if once:
            deliver_once()
            return
        while True:
            if not deliver_once():
                time.sleep(2)
