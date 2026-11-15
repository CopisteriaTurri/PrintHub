"""Connessione WebSocket degli agenti (PROTOCOL.md §5), end-to-end con il channel layer in memoria.

`TransactionTestCase` e non `TestCase`: il consumer usa il database da altri
thread, che non vedrebbero i dati dentro la transazione di un TestCase.
"""

from unittest import mock

from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TransactionTestCase, override_settings

from hub import services
from hub.models import Agent, Job
from printhub.asgi import application

from .helpers import PDF, PROFILES, TempMediaMixin, make_world

HELLO = {
    "type": "hello",
    "protocol": 1,
    "agent_version": "0.1.0",
    "os": "windows",
    "profiles": PROFILES,
}


class AgentSocketTests(TempMediaMixin, TransactionTestCase):
    def setUp(self):
        self.w = make_world()

    def connect(self, token=None):
        token = token or self.w["agent_token"]
        return WebsocketCommunicator(
            application,
            "/agent/v1",
            headers=[(b"authorization", f"Bearer {token}".encode())],
        )

    async def open(self, token=None, hello=HELLO):
        ws = self.connect(token)
        connected, _ = await ws.connect()
        self.assertTrue(connected)
        await ws.send_json_to(hello)
        welcome = await ws.receive_json_from()
        self.assertEqual(welcome["type"], "welcome", welcome)
        return ws, welcome

    @sync_to_async
    def create_job(self, key="k1", profile="fattura"):
        upload = SimpleUploadedFile("f.pdf", PDF)
        job, _ = services.create_job(self.w["key"], key, upload, {"profile": profile})
        return job.id

    @sync_to_async
    def job_state(self, job_id):
        return Job.objects.get(pk=job_id).state

    @sync_to_async
    def agent_online(self):
        return Agent.objects.get(pk=self.w["agent"].pk).online

    async def test_token_sbagliato_rifiutato(self):
        ws = self.connect("pha_falso")
        connected, _ = await ws.connect()
        self.assertFalse(connected)

    async def test_hello_e_welcome(self):
        ws, welcome = await self.open()
        self.assertEqual(welcome["agent"], "pc-banco")
        self.assertEqual(welcome["heartbeat_interval"], 20)
        self.assertNotIn("rejected_profiles", welcome)
        self.assertTrue(await self.agent_online())
        await ws.disconnect()
        self.assertFalse(await self.agent_online())

    async def test_lavori_in_coda_arrivano_dopo_il_welcome(self):
        job_id = await self.create_job()
        ws, _ = await self.open()
        offer = await ws.receive_json_from()
        self.assertEqual((offer["type"], offer["job_id"]), ("job.offer", job_id))
        await ws.disconnect()

    async def test_nuovo_lavoro_arriva_subito(self):
        ws, _ = await self.open()
        self.assertTrue(await ws.receive_nothing())
        job_id = await self.create_job()
        offer = await ws.receive_json_from(timeout=2)
        self.assertEqual(offer["job_id"], job_id)
        await ws.disconnect()

    async def test_ciclo_completo(self):
        job_id = await self.create_job()
        ws, _ = await self.open()
        await ws.receive_json_from()  # job.offer
        await ws.send_json_to({"type": "job.ack", "job_id": job_id})
        await ws.send_json_to(
            {"type": "job.update", "job_id": job_id, "state": "spooled", "os_job_id": 7}
        )
        await ws.send_json_to(
            {"type": "job.update", "job_id": job_id, "state": "completed"}
        )
        await ws.send_json_to({"type": "ping"})
        # Il pong arriva dopo che i messaggi precedenti sono stati elaborati.
        self.assertEqual(await ws.receive_json_from(), {"type": "pong"})
        self.assertEqual(await self.job_state(job_id), "completed")
        await ws.disconnect()

    async def test_serve_prima_l_hello(self):
        ws = self.connect()
        await ws.connect()
        await ws.send_json_to({"type": "job.ack", "job_id": "job_X"})
        error = await ws.receive_json_from()
        self.assertEqual(error["code"], "hello_required")
        output = await ws.receive_output()
        self.assertEqual(output["type"], "websocket.close")

    async def test_protocollo_non_supportato(self):
        ws = self.connect()
        await ws.connect()
        await ws.send_json_to({**HELLO, "protocol": 99})
        error = await ws.receive_json_from()
        self.assertEqual(error["code"], "protocol_unsupported")

    async def test_conflitto_di_profilo_nel_welcome(self):
        _, token = await sync_to_async(Agent.create)(self.w["tenant"], "pc-foto")
        ws, welcome = await self.open(
            token, {**HELLO, "profiles": [{"name": "fattura"}, {"name": "foto"}]}
        )
        self.assertEqual(
            welcome["rejected_profiles"],
            [{"name": "fattura", "code": "profile_conflict", "owner": "pc-banco"}],
        )
        await ws.disconnect()

    async def test_la_nuova_connessione_sostituisce_la_vecchia(self):
        old, _ = await self.open()
        new, _ = await self.open()
        output = await old.receive_output(timeout=2)
        self.assertEqual(output, {"type": "websocket.close", "code": 4409})
        self.assertTrue(await self.agent_online())
        await new.disconnect()

    async def test_annullamento_inoltrato_all_agente(self):
        job_id = await self.create_job()
        ws, _ = await self.open()
        await ws.receive_json_from()
        await ws.send_json_to({"type": "job.ack", "job_id": job_id})
        await ws.send_json_to({"type": "ping"})
        await ws.receive_json_from()  # pong: ack elaborato
        await sync_to_async(services.cancel_job)(self.w["key"], job_id)
        msg = await ws.receive_json_from(timeout=2)
        self.assertEqual(msg, {"type": "job.cancel", "job_id": job_id})
        await ws.disconnect()

    async def test_revoca_chiude_la_connessione(self):
        ws, _ = await self.open()
        await sync_to_async(services.revoke_agent)(self.w["agent"])
        output = await ws.receive_output(timeout=2)
        self.assertEqual(output, {"type": "websocket.close", "code": 4401})
        connected, _ = await self.connect().connect()
        self.assertFalse(connected)

    async def test_messaggi_sconosciuti_ignorati(self):
        ws, _ = await self.open()
        await ws.send_json_to({"type": "futuro.messaggio"})
        await ws.send_json_to({"type": "ping"})
        self.assertEqual(await ws.receive_json_from(), {"type": "pong"})
        await ws.disconnect()


@override_settings(PRINTHUB_RESYNC_INTERVAL=0.05, PRINTHUB_REOFFER_AFTER=0)
class ResyncTests(TempMediaMixin, TransactionTestCase):
    """Il controllo periodico consegna i lavori anche senza notifica."""

    def setUp(self):
        self.w = make_world()

    async def test_lavoro_arriva_anche_se_la_notifica_va_persa(self):
        ws = WebsocketCommunicator(
            application,
            "/agent/v1",
            headers=[(b"authorization", f"Bearer {self.w['agent_token']}".encode())],
        )
        await ws.connect()
        await ws.send_json_to(HELLO)
        await ws.receive_json_from()  # welcome

        @sync_to_async
        def create_without_notify():
            with mock.patch("hub.notify.job_created"):
                upload = SimpleUploadedFile("f.pdf", PDF)
                job, _ = services.create_job(
                    self.w["key"], "k1", upload, {"profile": "fattura"}
                )
            return job.id

        job_id = await create_without_notify()
        offer = await ws.receive_json_from(timeout=2)
        self.assertEqual(offer["job_id"], job_id)
        # Dopo l'ack non viene più riproposto.
        await ws.send_json_to({"type": "job.ack", "job_id": job_id})
        await ws.send_json_to({"type": "ping"})
        self.assertEqual(await ws.receive_json_from(), {"type": "pong"})
        self.assertTrue(await ws.receive_nothing(timeout=0.3))
        await ws.disconnect()

    async def test_revoca_senza_notifica_chiude_al_controllo_periodico(self):
        ws = WebsocketCommunicator(
            application,
            "/agent/v1",
            headers=[(b"authorization", f"Bearer {self.w['agent_token']}".encode())],
        )
        await ws.connect()
        await ws.send_json_to(HELLO)
        await ws.receive_json_from()  # welcome

        @sync_to_async
        def revoke_without_notify():
            with mock.patch("hub.notify.agent_close"):
                services.revoke_agent(self.w["agent"])
            upload = SimpleUploadedFile("f.pdf", PDF)
            with mock.patch("hub.notify.job_created"):
                services.create_job(self.w["key"], "k1", upload, {"profile": "fattura"})

        await revoke_without_notify()
        output = await ws.receive_output(timeout=2)
        self.assertEqual(output, {"type": "websocket.close", "code": 4401})
