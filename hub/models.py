"""Modelli di Print Hub (PROTOCOL.md §1).

Tutto appartiene a un Tenant: ogni query parte dal tenant della credenziale
usata, così un cliente dell'hub non può mai vedere i dati di un altro.
"""

from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from . import tokens


class Tenant(models.Model):
    name = models.CharField("nome", max_length=200)
    slug = models.SlugField(unique=True)
    max_copies = models.PositiveIntegerField("copie massime per lavoro", default=99)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "tenant"
        verbose_name_plural = "tenant"

    def __str__(self):
        return self.name


class ApiKey(models.Model):
    """Credenziale di un'applicazione client (CopyaGo, n8n…), §2."""

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="api_keys"
    )
    name = models.CharField("nome dell'applicazione", max_length=100)
    key_hash = models.CharField(max_length=64, unique=True, editable=False)
    # Primi caratteri della key, per riconoscerla nell'admin senza poterla usare.
    hint = models.CharField(max_length=12, editable=False)
    allowed_profiles = models.JSONField(
        "profili ammessi",
        default=list,
        blank=True,
        help_text="Nomi dei profili utilizzabili. Vuoto = tutti.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True, editable=False)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "API key"
        verbose_name_plural = "API key"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "name"], name="apikey_name_per_tenant"
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.hint}…)"

    @classmethod
    def create(cls, tenant, name, allowed_profiles=()):
        """Crea la key e restituisce anche il valore in chiaro, che non verrà
        più mostrato."""
        value, digest = tokens.new_secret(tokens.API_KEY_PREFIX)
        key = cls.objects.create(
            tenant=tenant,
            name=name,
            key_hash=digest,
            hint=value[:10],
            allowed_profiles=list(allowed_profiles),
        )
        return key, value

    def can_use(self, profile_name):
        return not self.allowed_profiles or profile_name in self.allowed_profiles


class Agent(models.Model):
    """Un CLIPrint installato su un PC (§5)."""

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="agents")
    name = models.SlugField("nome", max_length=64)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    hint = models.CharField(max_length=12, editable=False)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Stato della connessione, aggiornato dal consumer WebSocket.
    channel_name = models.CharField(max_length=200, blank=True, editable=False)
    connected_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_seen_at = models.DateTimeField(null=True, blank=True, editable=False)
    agent_version = models.CharField(max_length=40, blank=True, editable=False)
    os = models.CharField(max_length=40, blank=True, editable=False)
    printers = models.JSONField(default=list, blank=True, editable=False)

    class Meta:
        verbose_name = "agente"
        verbose_name_plural = "agenti"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "name"], name="agent_name_per_tenant"
            )
        ]

    def __str__(self):
        return self.name

    @classmethod
    def create(cls, tenant, name):
        value, digest = tokens.new_secret(tokens.AGENT_TOKEN_PREFIX)
        agent = cls.objects.create(
            tenant=tenant, name=name, token_hash=digest, hint=value[:10]
        )
        return agent, value

    @property
    def online(self):
        return bool(
            self.channel_name
            and not self.revoked_at
            and self.last_seen_at
            and self.last_seen_at
            >= timezone.now() - timedelta(seconds=settings.PRINTHUB_AGENT_OFFLINE_AFTER)
        )


class Profile(models.Model):
    """Un profilo dichiarato da un agente nel suo `hello` (§5.1).

    Il nome è univoco nel tenant: il primo agente che lo dichiara lo tiene.
    """

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="profiles"
    )
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="profiles")
    name = models.CharField(max_length=64)
    description = models.CharField(max_length=300, blank=True)
    printer = models.CharField(max_length=300, blank=True)
    accepts = models.JSONField(default=list)
    # Formato di §3.1: {nome: {"label", "default", "values": [{"value", "label"}]}}
    options = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "profilo"
        verbose_name_plural = "profili"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "name"], name="profile_name_per_tenant"
            )
        ]

    def __str__(self):
        return self.name


class Job(models.Model):
    """Un lavoro di stampa: un file con un profilo (§3, §4)."""

    class State(models.TextChoices):
        QUEUED = "queued", "in coda"
        DISPATCHED = "dispatched", "consegnato all'agente"
        SPOOLED = "spooled", "nella coda di stampa"
        COMPLETED = "completed", "stampato"
        FAILED = "failed", "fallito"
        CANCELED = "canceled", "annullato"
        EXPIRED = "expired", "scaduto"

    FINAL_STATES = {State.COMPLETED, State.FAILED, State.CANCELED, State.EXPIRED}
    # Lavori che l'agente deve ancora ricevere o confermare: vengono riproposti
    # a ogni connessione (l'agente scarta i doppioni col suo registro, §6).
    PENDING_STATES = {State.QUEUED, State.DISPATCHED}

    id = models.CharField(
        primary_key=True, max_length=30, default=tokens.new_job_id, editable=False
    )
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="jobs")
    api_key = models.ForeignKey(ApiKey, on_delete=models.PROTECT, related_name="jobs")
    idempotency_key = models.CharField(max_length=128)
    # Hash di file + meta: stessa chiave con contenuto diverso → 409 (§3.2).
    request_hash = models.CharField(max_length=64)

    # Nome del profilo al momento della richiesta: resta nello storico anche se
    # il profilo sparisce.
    profile_name = models.CharField(max_length=64)
    agent = models.ForeignKey(Agent, on_delete=models.PROTECT, related_name="jobs")
    printer = models.CharField(max_length=300, blank=True)
    options = models.JSONField(default=dict, blank=True)
    copies = models.PositiveIntegerField(default=1)
    title = models.CharField(max_length=200, blank=True)
    client_ref = models.CharField(max_length=200, blank=True, db_index=True)
    reprint_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL
    )

    file = models.FileField(upload_to="jobs/%Y/%m/", max_length=300, blank=True)
    file_sha256 = models.CharField(max_length=64)
    file_size = models.PositiveBigIntegerField()
    mime = models.CharField(max_length=50)
    file_purged_at = models.DateTimeField(null=True, blank=True)

    state = models.CharField(
        max_length=12, choices=State, default=State.QUEUED, db_index=True
    )
    error_code = models.CharField(max_length=40, blank=True)
    error_message = models.CharField(max_length=500, blank=True)
    os_job_id = models.CharField(max_length=40, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField()
    agent_seen_at = models.DateTimeField(null=True, blank=True, editable=False)
    # Ultima volta che il lavoro è stato proposto all'agente (job.offer).
    offered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "lavoro"
        verbose_name_plural = "lavori"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["api_key", "idempotency_key"], name="job_idempotency_per_key"
            ),
        ]
        indexes = [
            models.Index(
                fields=["agent", "state"],
                name="job_pending_per_agent",
                condition=Q(state__in=["queued", "dispatched"]),
            )
        ]

    def __str__(self):
        return self.id

    # Stati che possono risultare bloccati: l'agente li ha in carico ma non
    # dà riscontri da oltre la soglia.
    STALLED_STATES = (State.DISPATCHED, State.SPOOLED)

    @classmethod
    def stalled_cutoff(cls, now=None):
        """Prima di questo istante l'ultimo riscontro è troppo vecchio."""
        return (now or timezone.now()) - timedelta(
            seconds=settings.PRINTHUB_JOB_STALLED_AFTER
        )

    @classmethod
    def stalled_filter(cls, now=None):
        cutoff = cls.stalled_cutoff(now)
        return Q(state__in=cls.STALLED_STATES) & (
            Q(agent_seen_at__lt=cutoff)
            | Q(agent_seen_at__isnull=True, created_at__lt=cutoff)
        )

    @property
    def is_stalled(self):
        return (
            self.state in self.STALLED_STATES
            and (self.agent_seen_at or self.created_at) < self.stalled_cutoff()
        )

    @property
    def is_final(self):
        return self.state in self.FINAL_STATES
