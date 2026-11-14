from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from hub import services
from hub.models import MaintenanceStatus


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
        started = timezone.now()
        MaintenanceStatus.objects.get_or_create(pk=1)
        MaintenanceStatus.objects.filter(pk=1).filter(
            Q(last_attempt_at__isnull=True) | Q(last_attempt_at__lt=started)
        ).update(last_attempt_at=started, failed=True)
        expired = services.expire_overdue()
        purged = services.purge_files(days)
        events = services.purge_events()
        finished = timezone.now()
        MaintenanceStatus.objects.filter(pk=1).filter(
            Q(last_success_at__isnull=True) | Q(last_success_at__lt=finished)
        ).update(
            last_success_at=finished,
            expired_count=expired,
            purged_count=purged,
            events_count=events,
        )
        MaintenanceStatus.objects.filter(pk=1, last_attempt_at=started).update(
            failed=False
        )
        self.stdout.write(
            f"Lavori scaduti: {expired}. File cancellati: {purged}. Eventi cancellati: {events}."
        )
