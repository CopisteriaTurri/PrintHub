"""Regole degli agenti: profili dichiarati, stati, consegna dei lavori (§4–§6)."""

from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from hub import services
from hub.models import Agent, Job, Profile

from .helpers import PDF, PROFILES, TempMediaMixin, make_world


class ServicesTestCase(TempMediaMixin, TestCase):
    def setUp(self):
        self.w = make_world()
        self.agent = self.w["agent"]

    def job(self, profile="fattura", key="k1", **meta):
        upload = SimpleUploadedFile("f.pdf", PDF)
        job, _ = services.create_job(
            self.w["key"], key, upload, {"profile": profile, **meta}
        )
        return job

    def state(self, job):
        job.refresh_from_db()
        return job.state


class SyncProfilesTests(ServicesTestCase):
    def test_nome_gia_offerto_da_un_altro_agente(self):
        other, _ = Agent.create(self.w["tenant"], "pc-foto")
        rejected = services.sync_profiles(
            other, [{"name": "fattura"}, {"name": "foto"}]
        )
        self.assertEqual(
            rejected,
            [{"name": "fattura", "code": "profile_conflict", "owner": "pc-banco"}],
        )
        self.assertEqual(Profile.objects.get(name="fattura").agent, self.agent)
        self.assertEqual(Profile.objects.get(name="foto").agent, other)

    def test_profilo_non_piu_dichiarato_libera_il_nome(self):
        services.sync_profiles(
            self.agent, [p for p in PROFILES if p["name"] != "fattura"]
        )
        self.assertFalse(Profile.objects.filter(name="fattura").exists())
        other, _ = Agent.create(self.w["tenant"], "pc-foto")
        self.assertEqual(services.sync_profiles(other, [{"name": "fattura"}]), [])

    def test_profili_malformati_rifiutati_uno_per_uno(self):
        rejected = services.sync_profiles(
            self.agent,
            [
                {"name": "Fattura A4"},
                {"name": "ok"},
                {
                    "name": "opz",
                    "options": {"carta": {"default": "x", "values": [{"value": "y"}]}},
                },
                {"name": "testo", "accepts": ["text/plain"]},
            ],
        )
        self.assertEqual([r["code"] for r in rejected], ["invalid_profile"] * 3)
        self.assertTrue(Profile.objects.filter(name="ok").exists())

    def test_stesso_nome_in_un_altro_tenant_non_e_un_conflitto(self):
        other = make_world("altra")
        self.assertTrue(
            Profile.objects.filter(tenant=other["tenant"], name="fattura").exists()
        )


class OffersTests(ServicesTestCase):
    def test_offre_in_coda_e_consegnati_non_spooled(self):
        queued = self.job(key="1")
        dispatched = self.job(key="2")
        spooled = self.job(key="3")
        Job.objects.filter(pk=dispatched.pk).update(state="dispatched")
        Job.objects.filter(pk=spooled.pk).update(state="spooled")
        ids = [o["job_id"] for o in services.pending_offers(self.agent)]
        self.assertEqual(ids, [queued.id, dispatched.id])

    def test_offerta_contiene_file_e_opzioni(self):
        job = self.job("produzione", options={"carta": "a4-pesante"})
        offer = services.pending_offers(self.agent)[0]
        self.assertEqual(offer["type"], "job.offer")
        self.assertEqual(offer["options"], {"carta": "a4-pesante"})
        self.assertEqual(offer["file"]["url"], f"/agent/v1/jobs/{job.id}/file")
        self.assertEqual(offer["file"]["size"], len(PDF))
        self.assertEqual(len(offer["file"]["sha256"]), 64)

    def test_scaduti_non_vengono_offerti(self):
        job = self.job()
        Job.objects.filter(pk=job.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(services.pending_offers(self.agent), [])
        self.assertEqual(self.state(job), "expired")

    def test_agente_revocato_non_riceve_offerte(self):
        job = self.job()
        Agent.objects.filter(pk=self.agent.pk).update(revoked_at=timezone.now())
        self.assertIsNone(self.agent.revoked_at)
        self.assertEqual(services.pending_offers(self.agent), [])
        with override_settings(PRINTHUB_REOFFER_AFTER=0):
            self.assertEqual(services.stale_offers(self.agent), [])
        job.refresh_from_db()
        self.assertIsNone(job.offered_at)
        self.assertFalse(job.events.filter(kind="offer_attempt").exists())


class TransitionsTests(ServicesTestCase):
    def test_percorso_normale(self):
        job = self.job()
        self.assertTrue(services.ack_job(self.agent, job.id))
        self.assertTrue(
            services.update_job(self.agent, job.id, "spooled", {"os_job_id": 42})
        )
        self.assertTrue(services.update_job(self.agent, job.id, "completed"))
        job.refresh_from_db()
        self.assertEqual((job.state, job.os_job_id), ("completed", "42"))

    def test_non_si_torna_indietro(self):
        job = self.job()
        services.update_job(self.agent, job.id, "spooled")
        self.assertFalse(services.ack_job(self.agent, job.id))
        self.assertEqual(self.state(job), "spooled")

    def test_da_uno_stato_finale_non_si_esce(self):
        job = self.job()
        services.update_job(self.agent, job.id, "completed")
        self.assertFalse(services.update_job(self.agent, job.id, "failed"))
        self.assertEqual(self.state(job), "completed")

    def test_stato_sconosciuto_ignorato(self):
        job = self.job()
        self.assertFalse(services.update_job(self.agent, job.id, "stampato-forse"))

    def test_errore_registrato(self):
        job = self.job()
        services.update_job(
            self.agent,
            job.id,
            "failed",
            {"error": {"code": "uncertain", "message": "crash durante l'invio"}},
        )
        job.refresh_from_db()
        self.assertEqual(
            (job.error_code, job.error_message), ("uncertain", "crash durante l'invio")
        )

    def test_un_agente_non_tocca_i_lavori_di_un_altro(self):
        job = self.job()
        other, _ = Agent.create(self.w["tenant"], "pc-foto")
        self.assertFalse(services.update_job(other, job.id, "completed"))
        self.assertEqual(self.state(job), "queued")

    def test_agente_revocato_non_cambia_lo_stato(self):
        job = self.job()
        # Come nel consumer: l'oggetto in memoria è quello di prima della revoca.
        Agent.objects.filter(pk=self.agent.pk).update(revoked_at=timezone.now())
        self.assertIsNone(self.agent.revoked_at)
        self.assertFalse(services.ack_job(self.agent, job.id))
        self.assertFalse(services.update_job(self.agent, job.id, "completed"))
        self.assertFalse(services.reject_job(self.agent, job.id, "printer_offline"))
        services.reject_job(self.agent, job.id, "busy")
        job.refresh_from_db()
        self.assertEqual((job.state, job.agent_seen_at), ("queued", None))
        self.assertFalse(job.events.filter(source="agent").exists())

    def test_reject_busy_lascia_in_coda(self):
        job = self.job()
        services.reject_job(self.agent, job.id, "busy")
        self.assertEqual(self.state(job), "queued")
        services.reject_job(self.agent, job.id, "printer_offline", "spenta")
        job.refresh_from_db()
        self.assertEqual((job.state, job.error_code), ("failed", "printer_offline"))


class HelloTests(ServicesTestCase):
    def test_in_flight_riallinea_gli_stati(self):
        job = self.job()
        services.ack_job(self.agent, job.id)
        welcome, offers = services.hello(
            self.agent,
            {
                "type": "hello",
                "protocol": 1,
                "profiles": PROFILES,
                "in_flight": [{"job_id": job.id, "state": "spooled"}],
            },
        )
        self.assertEqual(welcome["type"], "welcome")
        self.assertEqual(self.state(job), "spooled")
        self.assertEqual(offers, [])  # già nella coda di stampa: non si ripropone

    def test_protocollo_non_supportato(self):
        with self.assertRaises(services.HubError) as ctx:
            services.hello(self.agent, {"type": "hello", "protocol": 2})
        self.assertEqual(ctx.exception.code, "protocol_unsupported")


class PurgeTests(ServicesTestCase):
    def test_cancella_i_file_vecchi_e_tiene_i_metadati(self):
        old = self.job(key="1")
        new = self.job(key="2")
        Job.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=8)
        )
        path = old.file.path
        updated_at = old.updated_at
        self.assertEqual(services.purge_files(days=7), 1)
        old.refresh_from_db()
        self.assertIsNotNone(old.file_purged_at)
        # La pulizia non è una transizione di stato: `updated_at` resta dov'è.
        self.assertEqual(old.updated_at, updated_at)
        self.assertFalse(old.file)
        import os

        self.assertFalse(os.path.exists(path))
        new.refresh_from_db()
        self.assertTrue(new.file)
        # Un lavoro senza file non si può più scaricare.
        self.assertIsNone(services.can_download(self.agent, old.id))


class PresenzaTests(ServicesTestCase):
    def test_confine_canale_e_revoca(self):
        from unittest.mock import patch

        from django.test import override_settings

        now = timezone.now()
        self.agent.channel_name = "canale"
        with (
            override_settings(PRINTHUB_AGENT_OFFLINE_AFTER=60),
            patch("hub.models.timezone.now", return_value=now),
        ):
            self.agent.last_seen_at = now - timedelta(seconds=60)
            self.assertTrue(self.agent.online)
            self.agent.last_seen_at -= timedelta(microseconds=1)
            self.assertFalse(self.agent.online)
            self.agent.last_seen_at = None
            self.assertFalse(self.agent.online)
            self.agent.last_seen_at = now
            self.agent.channel_name = ""
            self.assertFalse(self.agent.online)
            self.agent.channel_name = "canale"
            self.agent.revoked_at = now
            self.assertFalse(self.agent.online)

    def test_vecchio_canale_non_aggiorna_presenza_o_profili(self):
        services.agent_connected(self.agent, "vecchio")
        services.agent_connected(self.agent, "nuovo")
        self.agent.refresh_from_db()
        last_seen = self.agent.last_seen_at
        self.assertEqual(
            services.agent_seen(self.agent, channel_name="vecchio", printers=["x"]), 0
        )
        with self.assertRaises(services.HubError):
            services.hello(
                self.agent, {"protocol": 1, "profiles": []}, channel_name="vecchio"
            )
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.last_seen_at, last_seen)
        self.assertNotEqual(self.agent.printers, ["x"])
        self.assertTrue(Profile.objects.filter(agent=self.agent).exists())
        services.agent_disconnected(self.agent, "vecchio")
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.channel_name, "nuovo")


class LavoriBloccatiTests(ServicesTestCase):
    def test_riscontro_duplicato_e_messaggio_invalido(self):
        job = self.job()
        services.update_job(self.agent, job.id, Job.State.DISPATCHED)
        vecchio = timezone.now() - timedelta(seconds=301)
        Job.objects.filter(pk=job.pk).update(agent_seen_at=vecchio)
        job.refresh_from_db()
        self.assertTrue(job.is_stalled)
        self.assertTrue(
            Job.objects.filter(pk=job.pk).filter(Job.stalled_filter()).exists()
        )
        services.update_job(self.agent, job.id, Job.State.QUEUED)
        job.refresh_from_db()
        self.assertEqual(job.agent_seen_at, vecchio)
        updated_at = job.updated_at
        services.update_job(self.agent, job.id, Job.State.DISPATCHED)
        job.refresh_from_db()
        self.assertFalse(job.is_stalled)
        self.assertEqual(job.updated_at, updated_at)
        services.update_job(self.agent, job.id, Job.State.SPOOLED)
        Job.objects.filter(pk=job.pk).update(agent_seen_at=vecchio)
        job.refresh_from_db()
        self.assertTrue(job.is_stalled)
        services.purge_files(days=-1)
        job.refresh_from_db()
        self.assertTrue(job.is_stalled)
        services.update_job(self.agent, job.id, Job.State.COMPLETED)
        job.refresh_from_db()
        self.assertFalse(job.is_stalled)

    def test_confine_e_filtro_admin(self):
        from unittest.mock import patch

        from django.contrib import admin
        from django.test import RequestFactory

        from hub.admin import JobAdmin, StalledFilter

        job = self.job()
        now = timezone.now()
        Job.objects.filter(pk=job.pk).update(
            state=Job.State.DISPATCHED, agent_seen_at=now - timedelta(seconds=300)
        )
        job.refresh_from_db()
        with (
            self.settings(PRINTHUB_JOB_STALLED_AFTER=300),
            patch("hub.models.timezone.now", return_value=now),
        ):
            self.assertFalse(job.is_stalled)
            self.assertFalse(
                Job.objects.filter(pk=job.pk).filter(Job.stalled_filter()).exists()
            )
            Job.objects.filter(pk=job.pk).update(
                agent_seen_at=now - timedelta(seconds=301)
            )
            request = RequestFactory().get("/admin/", {"stalled": "yes"})
            filter_ = StalledFilter(
                request, request.GET.copy(), Job, JobAdmin(Job, admin.site)
            )
            self.assertTrue(
                filter_.queryset(request, Job.objects.all()).filter(pk=job.pk).exists()
            )
            Job.objects.filter(pk=job.pk).update(state=Job.State.QUEUED)
            self.assertFalse(
                filter_.queryset(request, Job.objects.all()).filter(pk=job.pk).exists()
            )
            Job.objects.filter(pk=job.pk).update(state=Job.State.COMPLETED)
            self.assertFalse(
                filter_.queryset(request, Job.objects.all()).filter(pk=job.pk).exists()
            )


class EventiTests(ServicesTestCase):
    def test_transizioni_e_offerte_senza_duplicati_di_stato(self):
        from hub.models import JobEvent

        job = self.job()
        services.pending_offers(self.agent)
        services.pending_offers(self.agent)
        services.ack_job(self.agent, job.id)
        services.ack_job(self.agent, job.id)
        self.assertEqual(
            list(job.events.values_list("kind", flat=True)),
            ["created", "offer_attempt", "offer_attempt", "state_changed"],
        )
        self.assertEqual(
            JobEvent.objects.filter(job=job, kind="state_changed").count(), 1
        )

    def test_scadenza_idempotente_e_retention(self):
        from hub.models import JobEvent

        job = self.job()
        Job.objects.filter(pk=job.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(services.expire_overdue(), 1)
        self.assertEqual(services.expire_overdue(), 0)
        self.assertEqual(job.events.filter(kind="state_changed").count(), 1)
        JobEvent.objects.filter(job=job).update(
            timestamp=timezone.now() - timedelta(days=91)
        )
        self.assertEqual(services.purge_events(), 2)
        self.assertTrue(Job.objects.filter(pk=job.pk).exists())

    def test_rollback_annulla_anche_evento(self):
        from django.db import transaction

        job = self.job()
        with self.assertRaises(ValueError), transaction.atomic():
            services.update_job(self.agent, job.id, Job.State.COMPLETED)
            raise ValueError("rollback")
        job.refresh_from_db()
        self.assertEqual(job.state, Job.State.QUEUED)
        self.assertEqual(job.events.count(), 1)
