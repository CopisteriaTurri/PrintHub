"""Versione dell'hub, mostrata nell'admin e in `/healthz`.

In produzione arriva da `APP_VERSION`, che il Dockerfile imposta dal build-arg
omonimo: chi costruisce l'immagine ha git e sa quale commit sta buildando
(nell'immagine `.git` non c'è). In sviluppo la si chiede a git. Senza nessuno
dei due, stringa vuota: l'admin semplicemente non la mostra.
"""

import os
import subprocess
from pathlib import Path


def get_version():
    version = os.environ.get("APP_VERSION", "").strip()
    if version:
        return version
    try:
        return subprocess.check_output(
            ["git", "describe", "--tags", "--always"],
            cwd=Path(__file__).resolve().parent.parent,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).strip()
    except (subprocess.SubprocessError, OSError):
        return ""


# Calcolata una volta per processo: `git describe` a ogni richiesta sarebbe un
# fork per pagina.
APP_VERSION = get_version()


def app_version(request):
    """Context processor: `APP_VERSION` nei template."""
    return {"APP_VERSION": APP_VERSION}
