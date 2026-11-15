"""Trasporto webhook: il worker consuma l'outbox creato da services.record_event
e limita ogni invio in un processo figlio."""

import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import uuid
from datetime import timedelta
from itertools import zip_longest
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import WebhookAttempt, WebhookDelivery

# Codici di uscita del processo figlio che invia la consegna: 0 successo,
# EXIT_TEMPORARY da ritentare, EXIT_PERMANENT senza retry. Qualunque altra
# uscita (avvio fallito, crash) vale come temporanea e viene ritentata.
EXIT_TEMPORARY = 75
EXIT_PERMANENT = 10

# Il processo figlio ha SEND_TIMEOUT secondi in tutto: la ricerca di un indirizzo
# raggiungibile ne usa al massimo CONNECT_BUDGET, il resto va a TLS e risposta.
SEND_TIMEOUT = 20
CONNECT_BUDGET = 10


def validate_destination(url, secret_ref):
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValueError("URL webhook non valido") from exc
    host = (parts.hostname or "").lower().rstrip(".")
    if (
        parts.scheme != "https"
        or not host
        or parts.username
        or parts.password
        or parts.fragment
        or port not in (None, 443)
    ):
        raise ValueError(
            "Usare un URL HTTPS senza credenziali, frammento o porta diversa da 443"
        )
    if host not in settings.PRINTHUB_WEBHOOK_ALLOWED_HOSTS:
        raise ValueError("Host webhook non presente nella allowlist")
    if not re.fullmatch(r"PRINTHUB_WEBHOOK_SECRET_[A-Z0-9_]+", secret_ref):
        raise ValueError("Riferimento segreto webhook non valido")
    return parts


def claim():
    now = timezone.now()
    candidates = (
        WebhookDelivery.objects.filter(
            completed_at__isnull=True, failed_at__isnull=True, next_attempt_at__lte=now
        )
        .filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now))
        .order_by("next_attempt_at", "pk")
        .values_list("pk", flat=True)[:100]
    )
    for pk in list(candidates):
        token = uuid.uuid4().hex
        updated = (
            WebhookDelivery.objects.filter(
                pk=pk,
                completed_at__isnull=True,
                failed_at__isnull=True,
                next_attempt_at__lte=now,
            )
            .filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now))
            .update(lease_until=now + timedelta(seconds=60), lease_token=token)
        )
        if updated:
            return pk, token
    return None


def deliver_once():
    claimed = claim()
    if not claimed:
        return False
    pk, token = claimed
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(settings.BASE_DIR / "manage.py"),
                "send_webhook",
                str(pk),
                token,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=SEND_TIMEOUT,
            check=False,
        )
        code = result.returncode
    except (subprocess.TimeoutExpired, OSError):
        code = EXIT_TEMPORARY
    delivery = WebhookDelivery.objects.filter(
        pk=pk, lease_token=token, completed_at__isnull=True, failed_at__isnull=True
    ).first()
    if delivery is None:
        return True
    now = timezone.now()
    attempts = delivery.attempts + 1
    values = {
        "attempts": attempts,
        "lease_until": None,
        "lease_token": "",
        "result": (
            "successo"
            if code == 0
            else "errore permanente" if code == EXIT_PERMANENT else "errore temporaneo"
        ),
    }
    if code == 0:
        values["completed_at"] = now
    elif code == EXIT_PERMANENT or attempts >= settings.PRINTHUB_WEBHOOK_MAX_ATTEMPTS:
        values["failed_at"] = now
    else:
        values["next_attempt_at"] = now + timedelta(
            seconds=min(3600, 2 ** min(attempts, 11))
        )
    with transaction.atomic():
        changed = WebhookDelivery.objects.filter(pk=pk, lease_token=token).update(
            **values
        )
        if changed:
            WebhookAttempt.objects.create(delivery_id=pk, result=values["result"])
    return True


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Si collega solo agli indirizzi già validati, provandoli in ordine: un
    indirizzo irraggiungibile (es. IPv6 senza rotta) non blocca gli altri."""

    def __init__(self, host, addresses):
        super().__init__(host, 443, timeout=5, context=ssl.create_default_context())
        self.addresses = list(addresses)

    def connect(self):
        error = None
        deadline = time.monotonic() + CONNECT_BUDGET
        for address in self.addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                sock = socket.create_connection(
                    (address, 443), timeout=min(5, remaining)
                )
            except OSError as exc:
                error = exc
                continue
            sock.settimeout(5)
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
            return
        raise error or OSError("nessun indirizzo webhook raggiungibile in tempo")


def attempt_order(addresses, attempt):
    """IPv6 e IPv4 alternati (come RFC 8305), così una famiglia irraggiungibile
    costa di norma un solo timeout; la partenza ruota a ogni retry."""
    by_family = ([], [])
    for address in sorted(addresses):
        by_family[ipaddress.ip_address(address).version == 4].append(address)
    order = [a for pair in zip_longest(*by_family) for a in pair if a is not None]
    shift = attempt % len(order)
    return order[shift:] + order[:shift]


def send(delivery):
    destination = delivery.destination
    if not destination.enabled:
        return 1
    try:
        parts = validate_destination(destination.url, destination.secret_ref)
        secret = os.environ.get(destination.secret_ref, "")
        if not secret:
            # Configurazione mancante: si ritenta, non si brucia la consegna.
            return EXIT_TEMPORARY
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)
        }
        if not addresses or any(
            not ipaddress.ip_address(address).is_global for address in addresses
        ):
            return 1
        body = json.dumps(
            delivery.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        timestamp = str(int(timezone.now().timestamp()))
        signature = hmac.new(
            secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        connection = PinnedHTTPSConnection(
            parts.hostname, attempt_order(addresses, delivery.attempts)
        )
        try:
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            connection.request(
                "POST",
                path,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "X-PrintHub-Timestamp": timestamp,
                    "X-PrintHub-Signature": "sha256=" + signature,
                    "X-PrintHub-Event": str(delivery.event_id),
                },
            )
            response = connection.getresponse()
            response.read(1024)
            if 200 <= response.status < 300:
                return 0
            return 75 if response.status == 429 or response.status >= 500 else 1
        finally:
            connection.close()
    except (ValueError, ssl.SSLCertVerificationError):
        # Prima di OSError: un certificato non valido è un errore permanente del
        # ricevente. Gli altri errori TLS (es. SSLEOFError) sono OSError e si
        # ritentano come gli errori di rete.
        return 1
    except (OSError, TimeoutError, http.client.HTTPException):
        return EXIT_TEMPORARY


def purge_deliveries():
    cutoff = timezone.now() - timedelta(days=settings.PRINTHUB_WEBHOOK_RETENTION_DAYS)
    count = 0
    while True:
        ids = list(
            WebhookDelivery.objects.filter(
                Q(completed_at__lt=cutoff) | Q(failed_at__lt=cutoff)
            )
            .order_by("pk")
            .values_list("pk", flat=True)[:1000]
        )
        if not ids:
            return count
        deleted, _ = WebhookDelivery.objects.filter(pk__in=ids).delete()
        count += deleted
