"""Regole degli agenti: profili dichiarati, stati, consegna dei lavori (§4–§6)."""

from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
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
        self.assertEqual(services.purge_files(days=7), 1)
        old.refresh_from_db()
        self.assertIsNotNone(old.file_purged_at)
        self.assertFalse(old.file)
        import os

        self.assertFalse(os.path.exists(path))
        new.refresh_from_db()
        self.assertTrue(new.file)
        # Un lavoro senza file non si può più scaricare.
        self.assertIsNone(services.can_download(self.agent, old.id))
