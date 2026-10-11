from django.core.management.base import BaseCommand, CommandError

from hub.models import ApiKey, Tenant


class Command(BaseCommand):
    help = "Crea una API key per un'applicazione client e la mostra una sola volta."

    def add_arguments(self, parser):
        parser.add_argument("tenant", help="slug del tenant")
        parser.add_argument("name", help="nome dell'applicazione, es. CopyaGo")
        parser.add_argument(
            "--profile",
            action="append",
            default=[],
            dest="profiles",
            help="profilo ammesso (ripetibile); senza, la key può usarli tutti",
        )

    def handle(self, tenant, name, profiles, **options):
        try:
            tenant_obj = Tenant.objects.get(slug=tenant)
        except Tenant.DoesNotExist:
            raise CommandError(f"tenant «{tenant}» inesistente") from None
        _, value = ApiKey.create(tenant_obj, name, profiles)
        self.stdout.write(
            f"API key per «{name}» (copiala ora, non verrà più mostrata):"
        )
        self.stdout.write(value)
