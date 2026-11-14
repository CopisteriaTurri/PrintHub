"""Logica di Print Hub, indipendente dal trasporto (HTTP o WebSocket).

Le view HTTP e il consumer degli agenti fanno solo da adattatori: leggono la
richiesta, chiamano queste funzioni e traducono il risultato. Così le regole
del protocollo (PROTOCOL.md) stanno in un posto solo e si testano senza rete.
"""

import hashlib
import json
import logging
import re
from datetime import UTC, timedelta

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from . import notify, tokens
from .models import Agent, ApiKey, Job, JobEvent, Profile

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
SUPPORTED_MEDIA = ("application/pdf", "image/png", "image/jpeg")
IDENTIFIER = re.compile(r"^[a-z0-9_-]{1,64}$")


class HubError(Exception):
    """Errore con un codice del protocollo (§7) e lo stato HTTP corrispondente."""

    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# --- Credenziali ------------------------------------------------------------


def authenticate_api_key(raw):
    if not raw or not raw.startswith(tokens.API_KEY_PREFIX):
        return None
    key = (
        ApiKey.objects.select_related("tenant")
        .filter(key_hash=tokens.hash_secret(raw), revoked_at__isnull=True)
        .first()
    )
    if key:
        ApiKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now())
    return key


def authenticate_agent(raw):
    if not raw or not raw.startswith(tokens.AGENT_TOKEN_PREFIX):
        return None
    return (
        Agent.objects.select_related("tenant")
        .filter(token_hash=tokens.hash_secret(raw), revoked_at__isnull=True)
        .first()
    )


def bearer(header_value):
    """Il token da un header `Authorization: Bearer <token>`."""
    if not header_value:
        return None
    scheme, _, value = header_value.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else None


# --- File -------------------------------------------------------------------


def sniff_mime(head):
    """Tipo del file dal contenuto, non dall'estensione (§8)."""
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


_EXTENSIONS = {"application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg"}


# --- Rappresentazioni JSON --------------------------------------------------


def iso(dt):
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z") if dt else None


def job_to_dict(job):
    error = (
        {"code": job.error_code, "message": job.error_message}
        if job.error_code
        else None
    )
    return {
        "id": job.id,
        "state": job.state,
        "profile": job.profile_name,
        "agent": job.agent.name,
        "printer": job.printer,
        "options": job.options,
        "copies": job.copies,
        "title": job.title,
        "client_ref": job.client_ref,
        "reprint_of": job.reprint_of_id,
        "created_by": job.api_key.name,
        "created_at": iso(job.created_at),
        "updated_at": iso(job.updated_at),
        "expires_at": iso(job.expires_at),
        "error": error,
    }


def profile_to_dict(profile):
    return {
        "name": profile.name,
        "agent": profile.agent.name,
        "description": profile.description,
        "accepts": profile.accepts,
        "online": profile.agent.online,
        "options": profile.options,
    }


def offer_message(job):
    return {
        "type": "job.offer",
        "job_id": job.id,
        "profile": job.profile_name,
        "options": job.options,
        "copies": job.copies,
        "title": job.title,
        "expires_at": iso(job.expires_at),
        "file": {
            "url": f"/agent/v1/jobs/{job.id}/file",
            "size": job.file_size,
            "mime": job.mime,
            "sha256": job.file_sha256,
        },
    }


# --- Profili visibili ai client ---------------------------------------------


def profiles_for(api_key):
    qs = (
        Profile.objects.filter(tenant=api_key.tenant)
        .select_related("agent")
        .order_by("name")
    )
    return [p for p in qs if api_key.can_use(p.name)]


# --- Creazione di un lavoro (§3.2) ------------------------------------------


def _str_field(meta, name, max_len, default=""):
    value = meta.get(name, default)
    if value is None:
        return default
    if not isinstance(value, str) or len(value) > max_len:
        raise HubError(
            "invalid_request",
            f"«{name}» deve essere un testo di al massimo {max_len} caratteri",
        )
    return value


def _int_field(meta, name, default, low, high):
    value = meta.get(name, default)
    if value is None:
        return default
    # bool è una sottoclasse di int in Python: `true` non deve valere 1 copia.
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not low <= value <= high
    ):
        raise HubError(
            "invalid_request", f"«{name}» deve essere un intero tra {low} e {high}"
        )
    return value


def resolve_options(profile, chosen):
    """Valida le opzioni scelte e aggiunge i default (§3.7)."""
    if not isinstance(chosen, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in chosen.items()
    ):
        raise HubError(
            "invalid_option", "«options» deve essere un oggetto nome → valore (testi)"
        )
    declared = profile.options or {}
    for name in chosen:
        if name not in declared:
            raise HubError(
                "invalid_option",
                f"il profilo «{profile.name}» non ha l'opzione «{name}»",
            )
    effective = {}
    for name, spec in declared.items():
        allowed = [v["value"] for v in spec.get("values", [])]
        value = chosen.get(name, spec.get("default"))
        if value not in allowed:
            raise HubError(
                "invalid_option",
                f"valore «{value}» non ammesso per «{name}» (ammessi: {', '.join(allowed)})",
            )
        effective[name] = value
    return effective


def _request_hash(file_sha256, meta):
    canonical = json.dumps(
        meta, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(f"{file_sha256}\n{canonical}".encode()).hexdigest()


def _read_upload(upload):
    """Legge il file a blocchi: hash, dimensione e primi byte per il tipo."""
    max_size = settings.PRINTHUB_MAX_FILE_SIZE
    if upload.size > max_size:
        raise HubError(
            "too_large", f"file oltre il limite di {max_size} byte", status=413
        )
    digest = hashlib.sha256()
    head = b""
    data = bytearray()
    for chunk in upload.chunks():
        if len(head) < 16:
            head += chunk[: 16 - len(head)]
        digest.update(chunk)
        data += chunk
    return bytes(data), digest.hexdigest(), head


def _existing_for_key(api_key, idempotency_key, request_hash):
    job = Job.objects.filter(api_key=api_key, idempotency_key=idempotency_key).first()
    if job is None:
        return None
    if job.request_hash != request_hash:
        raise HubError(
            "idempotency_conflict",
            "questa Idempotency-Key è già stata usata con un file o dati diversi",
            status=409,
        )
    return job


def record_event(job, kind, source, details=None):
    return JobEvent.objects.create(
        job=job, kind=kind, source=source, details=details or {}
    )


def create_job(api_key, idempotency_key, upload, meta, source="client"):
    """Crea un lavoro. Restituisce (job, created): `created` è False quando la
    stessa richiesta era già arrivata (stessa Idempotency-Key e stesso contenuto).

    `source` è chi ha originato il lavoro (`client` o, per una ristampa, `admin`)."""
    if not idempotency_key or len(idempotency_key) > 128:
        raise HubError(
            "invalid_request", "header Idempotency-Key obbligatorio (max 128 caratteri)"
        )
    if not isinstance(meta, dict):
        raise HubError("invalid_request", "«meta» deve essere un oggetto JSON")

    data, file_sha256, head = _read_upload(upload)
    request_hash = _request_hash(file_sha256, meta)

    # Prima l'idempotenza: un doppio invio restituisce il lavoro esistente anche
    # se nel frattempo il profilo è cambiato o l'agente è offline.
    existing = _existing_for_key(api_key, idempotency_key, request_hash)
    if existing:
        return existing, False

    tenant = api_key.tenant
    profile_name = _str_field(meta, "profile", 64)
    if not profile_name:
        raise HubError("invalid_request", "«profile» è obbligatorio")
    if not api_key.can_use(profile_name):
        raise HubError(
            "profile_not_allowed",
            f"questa API key non può usare il profilo «{profile_name}»",
            status=403,
        )
    profile = (
        Profile.objects.select_related("agent")
        .filter(tenant=tenant, name=profile_name)
        .first()
    )
    if profile is None:
        raise HubError(
            "unknown_profile",
            f"nessun agente offre il profilo «{profile_name}»",
            status=404,
        )

    mime = sniff_mime(head)
    if mime is None:
        raise HubError(
            "unsupported_media", "il file non è un PDF, PNG o JPEG", status=415
        )
    if mime not in profile.accepts:
        raise HubError(
            "unsupported_media",
            f"il profilo «{profile_name}» non accetta {mime} (accetta: {', '.join(profile.accepts)})",
            status=415,
        )

    options = resolve_options(profile, meta.get("options") or {})
    copies = _int_field(meta, "copies", 1, 1, tenant.max_copies)
    expires_in = _int_field(
        meta,
        "expires_in",
        settings.PRINTHUB_DEFAULT_EXPIRES_IN,
        10,
        settings.PRINTHUB_MAX_EXPIRES_IN,
    )
    title = _str_field(meta, "title", 200)
    client_ref = _str_field(meta, "client_ref", 200)
    reprint_of = None
    reprint_id = _str_field(meta, "reprint_of", 30)
    if reprint_id:
        reprint_of = Job.objects.filter(tenant=tenant, pk=reprint_id).first()
        if reprint_of is None:
            raise HubError(
                "invalid_request", f"«reprint_of»: lavoro {reprint_id} inesistente"
            )

    now = timezone.now()
    job = Job(
        tenant=tenant,
        api_key=api_key,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        profile_name=profile_name,
        agent=profile.agent,
        printer=profile.printer,
        options=options,
        copies=copies,
        title=title,
        client_ref=client_ref,
        reprint_of=reprint_of,
        file_sha256=file_sha256,
        file_size=len(data),
        mime=mime,
        created_at=now,
        expires_at=now + timedelta(seconds=expires_in),
    )
    try:
        with transaction.atomic():
            # Il nome del file è l'ID del lavoro, non quello originale: niente
            # dati personali nei nomi dei file sul disco.
            job.file.save(
                f"{job.id}.{_EXTENSIONS[mime]}", ContentFile(data), save=False
            )
            job.save(force_insert=True)
            record_event(job, "created", source, {"state": job.state})
            transaction.on_commit(lambda: notify.job_created(job))
    except IntegrityError:
        # Due richieste identiche in parallelo: ha vinto l'altra.
        job.file.delete(save=False)
        existing = _existing_for_key(api_key, idempotency_key, request_hash)
        if existing:
            return existing, False
        raise
    return job, True


# --- Lettura e annullamento -------------------------------------------------


def expire_overdue(queryset=None):
    """Segna `expired` i lavori in coda oltre la scadenza (§3.2). Restituisce quanti."""
    qs = Job.objects.all() if queryset is None else queryset
    now = timezone.now()
    ids = list(
        qs.filter(state=Job.State.QUEUED, expires_at__lte=now).values_list(
            "pk", flat=True
        )
    )
    count = 0
    for job_id in ids:
        with transaction.atomic():
            changed = Job.objects.filter(
                pk=job_id, state=Job.State.QUEUED, expires_at__lte=now
            ).update(
                state=Job.State.EXPIRED,
                error_code="expired",
                error_message="non partito entro la scadenza",
                updated_at=now,
            )
            if changed:
                job = Job.objects.get(pk=job_id)
                record_event(
                    job, "state_changed", "hub", {"from": "queued", "to": "expired"}
                )
                count += 1
    return count


def get_job(api_key, job_id):
    qs = Job.objects.select_related("agent", "api_key").filter(tenant=api_key.tenant)
    expire_overdue(qs.filter(pk=job_id))
    job = qs.filter(pk=job_id).first()
    if job is None:
        raise HubError("not_found", f"lavoro {job_id} inesistente", status=404)
    return job


def jobs_by_client_ref(api_key, client_ref, limit=50):
    qs = Job.objects.select_related("agent", "api_key").filter(
        tenant=api_key.tenant, client_ref=client_ref
    )
    expire_overdue(qs)
    return list(qs.order_by("-created_at")[:limit])


def cancel_job(api_key, job_id):
    """Annulla un lavoro (§3.4). In `spooled` la richiesta va all'agente, che
    prova a toglierlo dalla coda del sistema operativo; lo stato lo riporterà lui."""
    with transaction.atomic():
        job = get_job(api_key, job_id)
        job = (
            Job.objects.select_for_update()
            .select_related("agent", "api_key")
            .get(pk=job.pk)
        )
        if job.is_final:
            raise HubError(
                "not_cancelable",
                f"il lavoro è già {job.get_state_display()}",
                status=409,
            )
        was = job.state
        record_event(job, "cancel_requested", "client")
        if was in Job.PENDING_STATES:
            job.state = Job.State.CANCELED
            job.error_code = "canceled"
            job.error_message = "annullato dal client"
            job.save(
                update_fields=["state", "error_code", "error_message", "updated_at"]
            )
            record_event(job, "state_changed", "client", {"from": was, "to": job.state})
        if was in (Job.State.DISPATCHED, Job.State.SPOOLED):
            transaction.on_commit(lambda: notify.job_cancel(job))
    return job


# --- Agenti (§5) ------------------------------------------------------------


def _clean_options(raw):
    """Valida le opzioni dichiarate da un agente nel formato di §3.1."""
    if not isinstance(raw, dict):
        raise ValueError("options deve essere un oggetto")
    out = {}
    for name, spec in raw.items():
        if not IDENTIFIER.match(name) or not isinstance(spec, dict):
            raise ValueError(f"opzione «{name}» non valida")
        values = spec.get("values")
        if not isinstance(values, list) or not values:
            raise ValueError(f"opzione «{name}»: nessun valore")
        clean_values = []
        for v in values:
            if not isinstance(v, dict) or not IDENTIFIER.match(str(v.get("value", ""))):
                raise ValueError(f"opzione «{name}»: valore non valido")
            clean_values.append(
                {"value": v["value"], "label": str(v.get("label") or v["value"])[:100]}
            )
        default = spec.get("default")
        if default not in [v["value"] for v in clean_values]:
            raise ValueError(f"opzione «{name}»: default non tra i valori")
        out[name] = {
            "label": str(spec.get("label") or name)[:100],
            "default": default,
            "values": clean_values,
        }
    return out


def sync_profiles(agent, declared):
    """Allinea i profili dell'agente a quelli dichiarati nell'`hello` (§5.1).

    Restituisce i profili rifiutati. Un nome già offerto da un altro agente
    del tenant resta a quello (`profile_conflict`); profili malformati
    vengono rifiutati singolarmente senza bloccare gli altri.
    """
    rejected = []
    kept = set()
    if not isinstance(declared, list):
        declared = []
    for raw in declared:
        name = raw.get("name") if isinstance(raw, dict) else None
        if not isinstance(name, str) or not IDENTIFIER.match(name):
            rejected.append(
                {
                    "name": str(name),
                    "code": "invalid_profile",
                    "message": "nome non valido",
                }
            )
            continue
        try:
            accepts = raw.get("accepts") or list(SUPPORTED_MEDIA)
            if not isinstance(accepts, list) or not set(accepts) <= set(
                SUPPORTED_MEDIA
            ):
                raise ValueError("accepts contiene tipi non supportati")
            options = _clean_options(raw.get("options") or {})
        except ValueError as e:
            rejected.append(
                {"name": name, "code": "invalid_profile", "message": str(e)}
            )
            continue

        with transaction.atomic():
            owner = (
                Profile.objects.select_for_update()
                .select_related("agent")
                .filter(tenant=agent.tenant, name=name)
                .first()
            )
            if owner and owner.agent_id != agent.id:
                rejected.append(
                    {
                        "name": name,
                        "code": "profile_conflict",
                        "owner": owner.agent.name,
                    }
                )
                continue
            fields = {
                "description": str(raw.get("description") or "")[:300],
                "printer": str(raw.get("printer") or "")[:300],
                "accepts": accepts,
                "options": options,
            }
            if owner:
                for k, v in fields.items():
                    setattr(owner, k, v)
                owner.save()
            else:
                Profile.objects.create(
                    tenant=agent.tenant, agent=agent, name=name, **fields
                )
        kept.add(name)

    # I profili non più dichiarati spariscono: il nome torna libero per altri agenti.
    Profile.objects.filter(agent=agent).exclude(name__in=kept).delete()
    return rejected


def agent_connected(agent, channel_name):
    """Registra la connessione. Restituisce il canale della connessione
    precedente, se c'era, da chiudere: vale l'ultima."""
    with transaction.atomic():
        a = Agent.objects.select_for_update().get(pk=agent.pk)
        previous = a.channel_name if a.channel_name != channel_name else ""
        a.channel_name = channel_name
        a.connected_at = a.last_seen_at = timezone.now()
        a.save(update_fields=["channel_name", "connected_at", "last_seen_at"])
    return previous


def agent_disconnected(agent, channel_name):
    # Solo se è ancora la connessione registrata: una connessione vecchia che
    # si chiude dopo quella nuova non deve far risultare l'agente offline.
    Agent.objects.filter(pk=agent.pk, channel_name=channel_name).update(
        channel_name="", last_seen_at=timezone.now()
    )


def agent_seen(agent, channel_name=None, **fields):
    qs = Agent.objects.filter(pk=agent.pk, revoked_at__isnull=True)
    if channel_name is not None:
        qs = qs.filter(channel_name=channel_name)
    return qs.update(last_seen_at=timezone.now(), **fields)


@transaction.atomic
def hello(agent, msg, channel_name=None):
    """Gestisce il primo messaggio dell'agente. Restituisce (welcome, offerte)."""
    if msg.get("protocol") != PROTOCOL_VERSION:
        raise HubError(
            "protocol_unsupported", f"protocollo supportato: {PROTOCOL_VERSION}"
        )
    if channel_name is not None:
        current = (
            Agent.objects.select_for_update()
            .filter(pk=agent.pk, channel_name=channel_name, revoked_at__isnull=True)
            .exists()
        )
        if not current:
            raise HubError("connection_replaced", "connessione non più attiva")
    printers = msg.get("printers") if isinstance(msg.get("printers"), list) else []
    agent_seen(
        agent,
        channel_name=channel_name,
        agent_version=str(msg.get("agent_version") or "")[:40],
        os=str(msg.get("os") or "")[:40],
        printers=printers,
    )
    rejected = sync_profiles(agent, msg.get("profiles"))
    reconcile_in_flight(agent, msg.get("in_flight"))
    welcome = {
        "type": "welcome",
        "agent": agent.name,
        "heartbeat_interval": settings.PRINTHUB_HEARTBEAT_INTERVAL,
    }
    if rejected:
        welcome["rejected_profiles"] = rejected
    return welcome, pending_offers(agent)


def reconcile_in_flight(agent, in_flight):
    """Stati dei lavori che l'agente aveva in corso prima di disconnettersi (§5.1)."""
    if not isinstance(in_flight, list):
        return
    for item in in_flight:
        if isinstance(item, dict) and isinstance(item.get("job_id"), str):
            update_job(agent, item["job_id"], item.get("state"), item)


def pending_offers(agent, job_id=None):
    """I messaggi `job.offer` da mandare all'agente: lavori in coda o consegnati
    ma non ancora confermati come stampati. Riproporre un lavoro già ricevuto è
    sicuro: l'agente lo riconosce dal suo registro e non lo ristampa (§6)."""
    qs = Job.objects.filter(agent=agent, state__in=Job.PENDING_STATES)
    if job_id:
        qs = qs.filter(pk=job_id)
    expire_overdue(qs)
    jobs = list(qs.order_by("created_at"))
    if jobs:
        with transaction.atomic():
            Job.objects.filter(pk__in=[j.pk for j in jobs]).update(
                offered_at=timezone.now()
            )
            JobEvent.objects.bulk_create(
                [JobEvent(job=j, kind="offer_attempt", source="hub") for j in jobs]
            )
    return [offer_message(j) for j in jobs]


def stale_offers(agent):
    """Rete di sicurezza per le connessioni aperte: lavori in coda mai proposti,
    o proposti da più di PRINTHUB_REOFFER_AFTER secondi senza `job.ack`.

    Copre le notifiche perse (processo riavviato, più processi senza channel
    layer condiviso): un lavoro non resta mai fermo, al massimo arriva con
    qualche secondo di ritardo.
    """
    cutoff = timezone.now() - timedelta(seconds=settings.PRINTHUB_REOFFER_AFTER)
    stale = Job.objects.filter(agent=agent, state=Job.State.QUEUED).filter(
        Q(offered_at__isnull=True) | Q(offered_at__lt=cutoff)
    )
    ids = list(stale.values_list("pk", flat=True))
    return [o for job_id in ids for o in pending_offers(agent, job_id=job_id)]


# Transizioni ammesse dai messaggi dell'agente (§4). Da uno stato finale non si esce.
_AGENT_TRANSITIONS = {
    Job.State.QUEUED: {
        Job.State.DISPATCHED,
        Job.State.SPOOLED,
        Job.State.COMPLETED,
        Job.State.FAILED,
        Job.State.CANCELED,
    },
    Job.State.DISPATCHED: {
        Job.State.SPOOLED,
        Job.State.COMPLETED,
        Job.State.FAILED,
        Job.State.CANCELED,
    },
    Job.State.SPOOLED: {Job.State.COMPLETED, Job.State.FAILED, Job.State.CANCELED},
}


def update_job(agent, job_id, state, msg=None):
    """Applica uno stato riportato dall'agente. Restituisce True se è cambiato.

    Stati non validi o che tornerebbero indietro vengono ignorati e registrati
    nel log: un messaggio duplicato o in ritardo non deve far danni.
    """
    msg = msg or {}
    with transaction.atomic():
        job = Job.objects.select_for_update().filter(pk=job_id, agent=agent).first()
        if job is None:
            log.warning(
                "agente %s: aggiornamento per lavoro sconosciuto %s", agent, job_id
            )
            return False
        if state == job.state:
            if not job.is_final:
                job.agent_seen_at = timezone.now()
                job.save(update_fields=["agent_seen_at"])
            return False
        if state not in _AGENT_TRANSITIONS.get(job.state, set()):
            log.warning(
                "agente %s: transizione ignorata %s %s → %s",
                agent,
                job_id,
                job.state,
                state,
            )
            return False
        previous = job.state
        job.state = state
        job.agent_seen_at = timezone.now()
        fields = ["state", "updated_at", "agent_seen_at"]
        if msg.get("os_job_id") is not None:
            job.os_job_id = str(msg["os_job_id"])[:40]
            fields.append("os_job_id")
        error = msg.get("error")
        if state in (Job.State.FAILED, Job.State.CANCELED):
            error = error if isinstance(error, dict) else {}
            job.error_code = str(error.get("code") or state)[:40]
            job.error_message = str(error.get("message") or "")[:500]
            fields += ["error_code", "error_message"]
        job.save(update_fields=fields)
        record_event(job, "state_changed", "agent", {"from": previous, "to": state})
    return True


def ack_job(agent, job_id):
    return update_job(agent, job_id, Job.State.DISPATCHED)


def reject_job(agent, job_id, code, message=""):
    """`job.reject` (§5.2): `busy` lascia il lavoro in coda, gli altri lo fanno fallire."""
    if code == "busy":
        with transaction.atomic():
            job = Job.objects.select_for_update().filter(pk=job_id, agent=agent).first()
            if job and not job.is_final:
                record_event(job, "rejected", "agent", {"code": "busy"})
        return False
    return update_job(
        agent,
        job_id,
        Job.State.FAILED,
        {"error": {"code": code or "rejected", "message": message}},
    )


def can_download(agent, job_id):
    """Il file si serve solo all'agente assegnato e solo finché deve stamparlo."""
    return (
        Job.objects.filter(
            pk=job_id,
            agent=agent,
            state__in=Job.PENDING_STATES,
            file_purged_at__isnull=True,
        )
        .exclude(file="")
        .first()
    )


# --- Manutenzione -----------------------------------------------------------


def purge_files(days=None):
    """Cancella i file dei lavori più vecchi di `days` giorni (§8): restano i metadati."""
    days = settings.PRINTHUB_FILE_RETENTION_DAYS if days is None else days
    cutoff = timezone.now() - timedelta(days=days)
    count = 0
    for job in Job.objects.filter(
        created_at__lt=cutoff, file_purged_at__isnull=True
    ).exclude(file=""):
        job.file.delete(save=False)
        job.file_purged_at = timezone.now()
        # `updated_at` è l'ultima transizione di stato: la pulizia dei file non
        # deve farla sembrare recente nella dashboard.
        job.save(update_fields=["file", "file_purged_at"])
        count += 1
    return count


def purge_events():
    cutoff = timezone.now() - timedelta(days=settings.PRINTHUB_EVENT_RETENTION_DAYS)
    count = 0
    while True:
        ids = list(
            JobEvent.objects.filter(timestamp__lt=cutoff)
            .order_by("pk")
            .values_list("pk", flat=True)[:1000]
        )
        if not ids:
            return count
        deleted, _ = JobEvent.objects.filter(pk__in=ids).delete()
        count += deleted
