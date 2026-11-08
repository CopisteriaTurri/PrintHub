"""Probe isolato: il processo figlio viene terminato anche se una dipendenza si blocca."""

import logging
import subprocess
import sys

from django.conf import settings

log = logging.getLogger(__name__)


def ready():
    try:
        result = subprocess.run(
            [sys.executable, str(settings.BASE_DIR / "manage.py"), "check_readiness"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=settings.PRINTHUB_READINESS_TIMEOUT,
            check=False,
        )
        if result.returncode:
            log.warning("readiness: dipendenza non disponibile")
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        log.warning("readiness: probe scaduto o non avviabile")
        return False
