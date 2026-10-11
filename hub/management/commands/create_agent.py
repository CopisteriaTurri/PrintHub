from django.core.management.base import BaseCommand, CommandError

from hub.models import Agent, Tenant


class Command(BaseCommand):
    help = "Registra un agente CLIPrint e mostra il suo token una sola volta."

    def add_arguments(self, parser):
        parser.add_argument("tenant", help="slug del tenant")
        parser.add_argument("name", help="nome dell'agente, es. pc-banco")

    def handle(self, tenant, name, **options):
        try:
            tenant_obj = Tenant.objects.get(slug=tenant)
        except Tenant.DoesNotExist:
            raise CommandError(f"tenant «{tenant}» inesistente") from None
        _, value = Agent.create(tenant_obj, name)
        self.stdout.write(
            f"Token dell'agente «{name}» (copialo ora, non verrà più mostrato):"
        )
        self.stdout.write(value)
