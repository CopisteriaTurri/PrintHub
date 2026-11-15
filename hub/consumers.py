"""Connessione WebSocket degli agenti (PROTOCOL.md §5).

Il consumer traduce messaggi JSON in chiamate a `services` e viceversa. Ogni
accesso al database passa da `database_sync_to_async`: l'ORM di Django è
sincrono e non può girare direttamente dentro il loop asincrono.
"""

import asyncio
import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings

from . import notify, services
from .services import HubError

log = logging.getLogger(__name__)

# Codici di chiusura WebSocket applicativi (4000–4999).
CLOSE_UNAUTHORIZED = 4401
CLOSE_PROTOCOL = 4400
CLOSE_REPLACED = 4409


class AgentConsumer(AsyncJsonWebsocketConsumer):
    agent = None
    ready = False  # diventa True dopo un `hello` valido
    resync_task = None

    async def connect(self):
        # L'autenticazione è il token nell'header Authorization: un browser non
        # può impostarlo su una WebSocket, quindi non serve controllare l'Origin.
        headers = dict(self.scope.get("headers") or [])
        raw = services.bearer(headers.get(b"authorization", b"").decode("latin-1"))
        self.agent = await database_sync_to_async(services.authenticate_agent)(raw)
        if self.agent is None:
            # Chiudere prima di accept() fa fallire l'handshake con HTTP 403.
            await self.close()
            return
        await self.accept()
        await self.channel_layer.group_add(
            notify.group_name(self.agent.id), self.channel_name
        )
        previous = await database_sync_to_async(services.agent_connected)(
            self.agent, self.channel_name
        )
        if previous:
            # Vale l'ultima connessione: la vecchia (es. rimasta appesa dopo un
            # cambio di rete) viene chiusa.
            await self.channel_layer.send(
                previous, {"type": "hub.close", "reason": "replaced"}
            )
        log.info("agente %s connesso", self.agent)

    async def disconnect(self, code):
        if self.resync_task:
            self.resync_task.cancel()
        if self.agent is None:
            return
        await self.channel_layer.group_discard(
            notify.group_name(self.agent.id), self.channel_name
        )
        await database_sync_to_async(services.agent_disconnected)(
            self.agent, self.channel_name
        )
        log.info("agente %s disconnesso (%s)", self.agent, code)

    async def receive_json(self, content, **kwargs):
        if not isinstance(content, dict) or not isinstance(content.get("type"), str):
            await self.send_error(
                "invalid_message", "ogni messaggio deve avere un campo «type»"
            )
            return
        kind = content["type"]

        if kind == "ping":
            await self.send_json({"type": "pong"})
            await database_sync_to_async(services.agent_seen)(
                self.agent, channel_name=self.channel_name
            )
            return
        if kind == "hello":
            await self.on_hello(content)
            return
        if not self.ready:
            await self.send_error(
                "hello_required", "il primo messaggio deve essere «hello»"
            )
            await self.close(code=CLOSE_PROTOCOL)
            return

        handler = {
            "job.ack": self.on_ack,
            "job.reject": self.on_reject,
            "job.update": self.on_update,
            "agent.status": self.on_status,
            "pong": self.on_pong,
        }.get(kind)
        if handler is None:
            # Tipi sconosciuti si ignorano (§9): un agente più nuovo può mandare
            # messaggi che questo hub non conosce ancora.
            log.info("agente %s: messaggio sconosciuto %s ignorato", self.agent, kind)
            return
        await handler(content)

    async def send_error(self, code, message):
        await self.send_json({"type": "error", "code": code, "message": message})

    # --- Messaggi dall'agente -------------------------------------------------

    async def on_hello(self, msg):
        try:
            welcome, offers = await database_sync_to_async(services.hello)(
                self.agent, msg, channel_name=self.channel_name
            )
        except HubError as e:
            await self.send_error(e.code, e.message)
            await self.close(code=CLOSE_PROTOCOL)
            return
        self.ready = True
        await self.send_json(welcome)
        for offer in offers:
            await self.send_json(offer)
        if self.resync_task is None:
            self.resync_task = asyncio.create_task(self.resync_loop())

    async def resync_loop(self):
        """Controllo periodico nel database (`services.stale_offers`): i lavori
        arrivano anche se una notifica del channel layer va persa."""
        while True:
            await asyncio.sleep(settings.PRINTHUB_RESYNC_INTERVAL)
            try:
                # Rete di sicurezza se la notifica di chiusura è andata persa.
                if await database_sync_to_async(services.agent_revoked)(self.agent):
                    await self.close(code=CLOSE_UNAUTHORIZED)
                    return
                offers = await database_sync_to_async(services.stale_offers)(self.agent)
                for offer in offers:
                    await self.send_json(offer)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Un errore temporaneo (es. database irraggiungibile) non deve
                # chiudere la connessione: si riprova al giro successivo.
                log.exception("agente %s: controllo periodico non riuscito", self.agent)

    async def on_ack(self, msg):
        await database_sync_to_async(services.ack_job)(
            self.agent, str(msg.get("job_id"))
        )

    async def on_reject(self, msg):
        await database_sync_to_async(services.reject_job)(
            self.agent,
            str(msg.get("job_id")),
            msg.get("code"),
            str(msg.get("message") or ""),
        )

    async def on_update(self, msg):
        await database_sync_to_async(services.update_job)(
            self.agent, str(msg.get("job_id")), msg.get("state"), msg
        )

    async def on_status(self, msg):
        printers = msg.get("printers")
        if isinstance(printers, list):
            await database_sync_to_async(services.agent_seen)(
                self.agent, channel_name=self.channel_name, printers=printers
            )

    async def on_pong(self, msg):
        await database_sync_to_async(services.agent_seen)(
            self.agent, channel_name=self.channel_name
        )

    # --- Messaggi dal channel layer (type "hub.x" → metodo hub_x) -------------

    async def hub_job_new(self, event):
        if not self.ready:
            return  # i lavori in coda arriveranno con la risposta all'hello
        offers = await database_sync_to_async(services.pending_offers)(
            self.agent, job_id=event["job_id"]
        )
        for offer in offers:
            await self.send_json(offer)

    async def hub_job_cancel(self, event):
        await self.send_json({"type": "job.cancel", "job_id": event["job_id"]})

    async def hub_close(self, event):
        code = (
            CLOSE_REPLACED if event.get("reason") == "replaced" else CLOSE_UNAUTHORIZED
        )
        await self.close(code=code)
