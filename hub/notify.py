"""Messaggi verso le connessioni WebSocket degli agenti.

Le view HTTP girano in un processo qualsiasi; la connessione dell'agente può
stare in un altro. Il channel layer li collega (in memoria con un solo
processo, Valkey o Redis con più di uno): ogni agente connesso ascolta il
gruppo `agent-<id>`.
"""

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer


def group_name(agent_id):
    return f"agent-{agent_id}"


def _send(agent_id, message):
    layer = get_channel_layer()
    if layer is not None:
        async_to_sync(layer.group_send)(group_name(agent_id), message)


def job_created(job):
    _send(job.agent_id, {"type": "hub.job_new", "job_id": job.id})


def job_cancel(job):
    _send(job.agent_id, {"type": "hub.job_cancel", "job_id": job.id})


def agent_close(agent_id, reason):
    """Chiude la connessione dell'agente (token revocato, connessione sostituita)."""
    _send(agent_id, {"type": "hub.close", "reason": reason})
