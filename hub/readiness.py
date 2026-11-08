"""Probe isolato: il processo figlio viene terminato anche se una dipendenza si blocca."""

import logging
import subprocess
import sys
import threading
import time

from django.conf import settings

log = logging.getLogger(__name__)


_lock = threading.Lock()
_cached_at = 0.0
_cached_result = False


def ready():
    global _cached_at, _cached_result
    now = time.monotonic()
    if now - _cached_at < 5:
        return _cached_result
    if not _lock.acquire(blocking=False):
        return False
    try:
        now = time.monotonic()
        if now - _cached_at < 5:
            return _cached_result
        _cached_result = _probe()
        _cached_at = time.monotonic()
        return _cached_result
    finally:
        _lock.release()


def _probe():
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
