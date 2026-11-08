from django.core.management.base import BaseCommand

from hub import services


class Command(BaseCommand):
    help = (
        "Manutenzione periodica: segna scaduti i lavori non partiti e cancella i "
        "file più vecchi della retention (PRINTHUB_FILE_RETENTION_DAYS)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days", type=int, default=None, help="retention dei file in giorni"
        )

    def handle(self, days, **options):
        expired = services.expire_overdue()
        purged = services.purge_files(days)
        events = services.purge_events()
        self.stdout.write(
            f"Lavori scaduti: {expired}. File cancellati: {purged}. Eventi cancellati: {events}."
        )
