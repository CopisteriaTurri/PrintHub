"""API dei client (PROTOCOL.md §3) e download dei file per gli agenti (§5.2)."""

import json
from datetime import timedelta
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from hub.models import ApiKey, Job

from .helpers import PDF, PNG, TempMediaMixin, make_world


class ApiTestCase(TempMediaMixin, TestCase):
    def setUp(self):
        self.w = make_world()
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.w['key_value']}"}

    def post_job(self, meta, content=PDF, key="chiave-1", auth=None):
        headers = dict(auth if auth is not None else self.auth)
        if key is not None:
            headers["HTTP_IDEMPOTENCY_KEY"] = key
        data = {"file": SimpleUploadedFile("documento.pdf", content)}
        data["meta"] = meta if isinstance(meta, str) else json.dumps(meta)
        return self.client.post("/api/v1/jobs", data, **headers)

    def assertError(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content)
        self.assertEqual(response.json()["error"]["code"], code)


class AuthTests(ApiTestCase):
    def test_senza_key(self):
        self.assertError(self.client.get("/api/v1/profiles"), 401, "unauthorized")

    def test_key_sbagliata(self):
        r = self.client.get("/api/v1/profiles", HTTP_AUTHORIZATION="Bearer phk_falsa")
        self.assertError(r, 401, "unauthorized")

    def test_key_revocata(self):
        ApiKey.objects.update(revoked_at=timezone.now())
        self.assertError(
            self.client.get("/api/v1/profiles", **self.auth), 401, "unauthorized"
        )

    def test_il_token_di_un_agente_non_vale_come_api_key(self):
        auth = {"HTTP_AUTHORIZATION": f"Bearer {self.w['agent_token']}"}
        self.assertError(
            self.client.get("/api/v1/profiles", **auth), 401, "unauthorized"
        )

    def test_uso_registrato(self):
        self.client.get("/api/v1/profiles", **self.auth)
        self.w["key"].refresh_from_db()
        self.assertIsNotNone(self.w["key"].last_used_at)


class ProfilesTests(ApiTestCase):
    def test_presenza_scaduta_nella_lista(self):
        agent = self.w["agent"]
        agent.channel_name = "canale"
        agent.last_seen_at = timezone.now() - timedelta(seconds=61)
        agent.save(update_fields=["channel_name", "last_seen_at"])
        with self.settings(PRINTHUB_AGENT_OFFLINE_AFTER=60):
            response = self.client.get("/api/v1/profiles", **self.auth)
        self.assertFalse(response.json()["profiles"][0]["online"])

    def test_elenco(self):
        r = self.client.get("/api/v1/profiles", **self.auth)
        self.assertEqual(r.status_code, 200)
        profiles = {p["name"]: p for p in r.json()["profiles"]}
        self.assertEqual(set(profiles), {"fattura", "produzione", "etichetta"})
        self.assertEqual(profiles["fattura"]["agent"], "pc-banco")
        self.assertFalse(profiles["fattura"]["online"])
        self.assertEqual(
            profiles["produzione"]["options"]["carta"]["default"], "a4-normale"
        )

    def test_key_limitata_vede_solo_i_suoi_profili(self):
        _, value = ApiKey.create(self.w["tenant"], "telefono", ["etichetta"])
        r = self.client.get("/api/v1/profiles", HTTP_AUTHORIZATION=f"Bearer {value}")
        self.assertEqual([p["name"] for p in r.json()["profiles"]], ["etichetta"])


class CreateJobTests(ApiTestCase):
    def test_crea_lavoro(self):
        with mock.patch("hub.notify.job_created") as notified:
            with self.captureOnCommitCallbacks(execute=True):
                r = self.post_job(
                    {"profile": "fattura", "title": "Fattura 12", "client_ref": "f:12"}
                )
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertTrue(body["id"].startswith("job_"))
        self.assertEqual(body["state"], "queued")
        self.assertEqual(body["agent"], "pc-banco")
        self.assertEqual(body["printer"], "HP LaserJet Pro")
        self.assertEqual(body["created_by"], "CopyaGo")
        self.assertEqual(body["copies"], 1)
        notified.assert_called_once()

        job = Job.objects.get(pk=body["id"])
        self.assertEqual(job.mime, "application/pdf")
        self.assertEqual(job.file_size, len(PDF))
        # Il nome del file sul disco è l'ID, non quello originale.
        self.assertIn(job.id, job.file.name)
        self.assertNotIn("documento", job.file.name)
        with job.file.open("rb") as f:
            self.assertEqual(f.read(), PDF)

    def test_opzioni_con_default(self):
        r = self.post_job({"profile": "produzione"})
        self.assertEqual(r.json()["options"], {"carta": "a4-normale"})
        r = self.post_job(
            {"profile": "produzione", "options": {"carta": "a4-pesante"}}, key="2"
        )
        self.assertEqual(r.json()["options"], {"carta": "a4-pesante"})

    def test_stessa_richiesta_due_volte_un_solo_lavoro(self):
        meta = {"profile": "fattura", "title": "Fattura 12"}
        first = self.post_job(meta)
        second = self.post_job(meta)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(Job.objects.count(), 1)

    def test_stessa_chiave_contenuto_diverso(self):
        self.post_job({"profile": "fattura"})
        self.assertError(
            self.post_job({"profile": "fattura", "copies": 2}),
            409,
            "idempotency_conflict",
        )
        self.assertError(
            self.post_job({"profile": "fattura"}, content=PDF + b"x"),
            409,
            "idempotency_conflict",
        )

    def test_la_chiave_vale_per_singola_api_key(self):
        _, other = ApiKey.create(self.w["tenant"], "n8n")
        self.post_job({"profile": "fattura"})
        r = self.post_job(
            {"profile": "fattura"}, auth={"HTTP_AUTHORIZATION": f"Bearer {other}"}
        )
        self.assertEqual(r.status_code, 201)
        self.assertEqual(Job.objects.count(), 2)

    def test_idempotency_key_obbligatoria(self):
        self.assertError(
            self.post_job({"profile": "fattura"}, key=None), 400, "invalid_request"
        )

    def test_meta_non_json(self):
        self.assertError(self.post_job("{non json"), 400, "invalid_request")

    def test_profilo_sconosciuto(self):
        self.assertError(
            self.post_job({"profile": "fototessera"}), 404, "unknown_profile"
        )

    def test_profilo_non_ammesso(self):
        _, value = ApiKey.create(self.w["tenant"], "telefono", ["etichetta"])
        r = self.post_job(
            {"profile": "fattura"}, auth={"HTTP_AUTHORIZATION": f"Bearer {value}"}
        )
        self.assertError(r, 403, "profile_not_allowed")

    def test_file_non_supportato(self):
        self.assertError(
            self.post_job({"profile": "fattura"}, content=b"Fattura n. 12"),
            415,
            "unsupported_media",
        )

    def test_tipo_non_accettato_dal_profilo(self):
        self.assertError(
            self.post_job({"profile": "fattura"}, content=PNG), 415, "unsupported_media"
        )

    def test_opzione_non_valida(self):
        r = self.post_job({"profile": "produzione", "options": {"carta": "a5"}})
        self.assertError(r, 400, "invalid_option")
        self.assertIn("a4-pesante", r.json()["error"]["message"])
        r = self.post_job(
            {"profile": "fattura", "options": {"carta": "a4-normale"}}, key="2"
        )
        self.assertError(r, 400, "invalid_option")
        self.assertEqual(Job.objects.count(), 0)

    def test_copie_fuori_limite(self):
        self.assertError(
            self.post_job({"profile": "fattura", "copies": 0}), 400, "invalid_request"
        )
        self.assertError(
            self.post_job({"profile": "fattura", "copies": 100}, key="2"),
            400,
            "invalid_request",
        )
        # `true` non è un numero di copie, anche se in Python bool è un int.
        self.assertError(
            self.post_job({"profile": "fattura", "copies": True}, key="3"),
            400,
            "invalid_request",
        )

    def test_file_troppo_grande(self):
        with self.settings(PRINTHUB_MAX_FILE_SIZE=10):
            self.assertError(self.post_job({"profile": "fattura"}), 413, "too_large")

    def test_campi_sconosciuti_ignorati(self):
        r = self.post_job({"profile": "fattura", "futuro": 1})
        self.assertEqual(r.status_code, 201)

    def test_ristampa(self):
        first = self.post_job({"profile": "fattura"}).json()
        r = self.post_job(
            {"profile": "fattura", "reprint_of": first["id"]}, key="ristampa-1"
        )
        self.assertEqual(r.json()["reprint_of"], first["id"])
        self.assertError(
            self.post_job(
                {"profile": "fattura", "reprint_of": "job_NONESISTE"}, key="x"
            ),
            400,
            "invalid_request",
        )


class ReadJobTests(ApiTestCase):
    def test_dettaglio(self):
        job_id = self.post_job({"profile": "fattura"}).json()["id"]
        r = self.client.get(f"/api/v1/jobs/{job_id}", **self.auth)
        self.assertEqual(r.json()["state"], "queued")

    def test_scadenza(self):
        job_id = self.post_job({"profile": "fattura", "expires_in": 10}).json()["id"]
        Job.objects.filter(pk=job_id).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        body = self.client.get(f"/api/v1/jobs/{job_id}", **self.auth).json()
        self.assertEqual(body["state"], "expired")
        self.assertEqual(body["error"]["code"], "expired")

    def test_per_client_ref(self):
        self.post_job({"profile": "fattura", "client_ref": "fatture.Fattura:1"})
        self.post_job(
            {"profile": "fattura", "client_ref": "fatture.Fattura:2"}, key="2"
        )
        r = self.client.get(
            "/api/v1/jobs", {"client_ref": "fatture.Fattura:1"}, **self.auth
        )
        self.assertEqual(len(r.json()["jobs"]), 1)
        self.assertError(
            self.client.get("/api/v1/jobs", **self.auth), 400, "invalid_request"
        )

    def test_isolamento_tra_tenant(self):
        job_id = self.post_job({"profile": "fattura"}).json()["id"]
        other = make_world("altra-copisteria")
        auth = {"HTTP_AUTHORIZATION": f"Bearer {other['key_value']}"}
        self.assertError(
            self.client.get(f"/api/v1/jobs/{job_id}", **auth), 404, "not_found"
        )
        self.assertError(
            self.client.post(f"/api/v1/jobs/{job_id}/cancel", **auth), 404, "not_found"
        )


class CancelTests(ApiTestCase):
    def cancel(self, job_id):
        return self.client.post(f"/api/v1/jobs/{job_id}/cancel", **self.auth)

    def test_annulla_in_coda(self):
        job_id = self.post_job({"profile": "fattura"}).json()["id"]
        r = self.cancel(job_id)
        self.assertEqual(r.json()["state"], "canceled")

    def test_annulla_spooled_avvisa_l_agente_e_non_cambia_stato(self):
        job_id = self.post_job({"profile": "fattura"}).json()["id"]
        Job.objects.filter(pk=job_id).update(state="spooled")
        with mock.patch("hub.notify.job_cancel") as notified:
            with self.captureOnCommitCallbacks(execute=True):
                r = self.cancel(job_id)
        self.assertEqual(r.json()["state"], "spooled")
        notified.assert_called_once()

    def test_stato_finale_non_annullabile(self):
        job_id = self.post_job({"profile": "fattura"}).json()["id"]
        Job.objects.filter(pk=job_id).update(state="completed")
        self.assertError(self.cancel(job_id), 409, "not_cancelable")


class AgentFileTests(ApiTestCase):
    def download(self, job_id, token):
        return self.client.get(
            f"/agent/v1/jobs/{job_id}/file", HTTP_AUTHORIZATION=f"Bearer {token}"
        )

    def test_scarica_il_file(self):
        job_id = self.post_job({"profile": "fattura"}).json()["id"]
        r = self.download(job_id, self.w["agent_token"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertEqual(r.content, PDF)

    def test_altro_agente_non_puo(self):
        job_id = self.post_job({"profile": "fattura"}).json()["id"]
        from hub.models import Agent

        _, token = Agent.create(self.w["tenant"], "pc-foto")
        self.assertEqual(self.download(job_id, token).status_code, 404)

    def test_senza_token(self):
        job_id = self.post_job({"profile": "fattura"}).json()["id"]
        self.assertEqual(
            self.client.get(f"/agent/v1/jobs/{job_id}/file").status_code, 401
        )
        # Una API key non è un token di agente.
        self.assertEqual(self.download(job_id, self.w["key_value"]).status_code, 401)

    def test_dopo_la_stampa_non_si_scarica_piu(self):
        job_id = self.post_job({"profile": "fattura"}).json()["id"]
        Job.objects.filter(pk=job_id).update(state="spooled")
        self.assertEqual(self.download(job_id, self.w["agent_token"]).status_code, 404)


class HealthTests(TestCase):
    def test_healthz(self):
        with mock.patch("hub.views.APP_VERSION", "abc1234"):
            data = self.client.get("/healthz").json()
        self.assertEqual(data, {"status": "ok", "version": "abc1234"})
