from django.core.management.base import BaseCommand

from hub.models import Tenant


class Command(BaseCommand):
    help = "Crea un tenant (un cliente dell'hub, es. una copisteria)."

    def add_arguments(self, parser):
        parser.add_argument("slug")
        parser.add_argument("name")

    def handle(self, slug, name, **options):
        tenant = Tenant.objects.create(slug=slug, name=name)
        self.stdout.write(f"Tenant «{tenant.name}» creato ({tenant.slug}).")
