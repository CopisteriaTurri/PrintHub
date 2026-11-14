from django.core.management.base import BaseCommand
from django.utils import timezone

from hub.models import WebhookDelivery
from hub.webhooks import EXIT_PERMANENT, send


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("delivery", type=int)
        parser.add_argument("token")

    def handle(self, delivery, token, **options):
        item = (
            WebhookDelivery.objects.select_related("destination")
            .filter(
                pk=delivery,
                lease_token=token,
                lease_until__gt=timezone.now(),
                completed_at__isnull=True,
                failed_at__isnull=True,
            )
            .first()
        )
        if not item:
            raise SystemExit(1)
        code = send(item)
        # 1 di `send()` significa "errore permanente del ricevente"; un crash
        # del comando esce con 1, quindi il worker deve poterli distinguere.
        raise SystemExit(EXIT_PERMANENT if code == 1 else code)
