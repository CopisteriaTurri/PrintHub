import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hub import services
from hub.models import Agent, Job, Profile, Tenant

from .helpers import PDF, TempMediaMixin, make_world

# Senza collectstatic non c'è il manifest che lo storage di produzione richiede.
STORAGE_SENZA_MANIFEST = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


@override_settings(STORAGES=STORAGE_SENZA_MANIFEST)
class OperationsTests(TempMediaMixin, TestCase):
    def setUp(self):
        self.w = make_world()
        self.superuser = get_user_model().objects.create_superuser(
            "admin", "a@example.invalid", "pw"
        )
        self.staff = get_user_model().objects.create_user(
            "staff", password="pw", is_staff=True
        )
        self.job, _ = services.create_job(
            self.w["key"],
            "original",
            SimpleUploadedFile("x.pdf", PDF),
            {"profile": "fattura"},
        )
        self.url = reverse(
            "operations-reprint", args=[self.w["tenant"].pk, self.job.pk]
        )

    def _token(self):
        response = self.client.get(self.url)
        return re.search(
            r'name="token" value="([^"]+)"', response.content.decode()
        ).group(1)

    def _presenza(self, response):
        """La sezione Agenti, senza i select dei filtri."""
        html = response.content.decode()
        return html.split("<h2>Agenti</h2>")[1].split("<form")[0]

    def test_permessi_filtri_e_get_senza_effetti(self):
        self.assertEqual(self.client.get(reverse("operations")).status_code, 302)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("operations")).status_code, 403)
        self.client.force_login(self.superuser)
        for filtro, valore in (("tenant", "x"), ("agent", "x"), ("state", "x")):
            self.assertEqual(
                self.client.get(reverse("operations"), {filtro: valore}).status_code,
                400,
            )
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertEqual(Job.objects.count(), 1)
        self.assertEqual(
            self.client.get(
                reverse("operations-cancel", args=[self.w["tenant"].pk, self.job.pk])
            ).status_code,
            405,
        )
        self.assertEqual(
            self.client.get(
                reverse(
                    "operations-reprint", args=[self.w["tenant"].pk + 100, self.job.pk]
                )
            ).status_code,
            404,
        )

    def test_csrf_obbligatorio_sulle_azioni(self):
        client = self.client_class(enforce_csrf_checks=True)
        client.force_login(self.superuser)
        self.assertEqual(
            client.post(
                reverse("operations-cancel", args=[self.w["tenant"].pk, self.job.pk])
            ).status_code,
            403,
        )
        self.assertEqual(client.post(self.url, {"token": "x"}).status_code, 403)

    def test_filtri_anche_sulla_presenza(self):
        altro = Tenant.objects.create(slug="altro", name="Altro")
        Agent.create(altro, "pc-altro-tenant")
        self.client.force_login(self.superuser)
        response = self.client.get(
            reverse("operations"), {"tenant": self.w["tenant"].pk}
        )
        self.assertIn("pc-banco", self._presenza(response))
        self.assertNotIn("pc-altro-tenant", self._presenza(response))
        response = self.client.get(reverse("operations"), {"agent": self.w["agent"].pk})
        self.assertNotIn("pc-altro-tenant", self._presenza(response))
        response = self.client.get(reverse("operations"))
        self.assertIn("pc-altro-tenant", self._presenza(response))

    def test_errori_recenti_finestra_e_link_al_filtro(self):
        recente, _ = services.create_job(
            self.w["key"],
            "errore-recente",
            SimpleUploadedFile("x.pdf", PDF),
            {"profile": "fattura"},
        )
        Job.objects.filter(pk=recente.pk).update(state=Job.State.FAILED)
        vecchio, _ = services.create_job(
            self.w["key"],
            "errore-vecchio",
            SimpleUploadedFile("x.pdf", PDF),
            {"profile": "fattura"},
        )
        Job.objects.filter(pk=vecchio.pk).update(
            state=Job.State.FAILED, updated_at=timezone.now() - timedelta(hours=48)
        )
        self.client.force_login(self.superuser)
        response = self.client.get(
            reverse("operations"), {"tenant": self.w["tenant"].pk}
        )
        self.assertContains(response, "Falliti (ultime 24 h): 1")
        self.assertContains(
            response,
            f"?tenant={self.w['tenant'].pk}&amp;agent=&amp;state=failed&amp;recent=1",
        )
        response = self.client.get(
            reverse("operations"),
            {"tenant": self.w["tenant"].pk, "state": "failed", "recent": "1"},
        )
        self.assertEqual([job.pk for job in response.context["jobs"]], [recente.pk])

    def test_ristampa_idempotente_file_indipendente_e_key_revocata(self):
        self.client.force_login(self.superuser)
        token = self._token()
        self.client.post(self.url, {"token": token})
        self.client.post(self.url, {"token": token})
        self.assertEqual(Job.objects.count(), 2)
        new = Job.objects.exclude(pk=self.job.pk).get()
        self.assertEqual(new.reprint_of_id, self.job.pk)
        self.assertNotEqual(new.file.name, self.job.file.name)
        self.assertTrue(new.events.filter(kind="reprint_requested").exists())
        self.assertEqual(new.events.get(kind="created").source, "admin")
        self.w["key"].revoked_at = timezone.now()
        self.w["key"].save()
        self.client.post(self.url, {"token": self._token()})
        self.assertEqual(Job.objects.count(), 2)

    def test_ristampa_file_mancante_o_profilo_sparito(self):
        self.client.force_login(self.superuser)
        altro, _ = services.create_job(
            self.w["key"],
            "secondo",
            SimpleUploadedFile("y.pdf", PDF),
            {"profile": "fattura"},
        )
        Profile.objects.filter(tenant=self.w["tenant"], name="fattura").delete()
        url = reverse("operations-reprint", args=[altro.tenant_id, altro.pk])
        token = re.search(
            r'name="token" value="([^"]+)"',
            self.client.get(url).content.decode(),
        ).group(1)
        response = self.client.post(url, {"token": token}, follow=True)
        self.assertEqual(Job.objects.count(), 2)
        self.assertContains(response, "Nessun agente offre il profilo «fattura»")

        token = self._token()
        self.job.file.delete(save=False)
        self.job.file_purged_at = timezone.now()
        self.job.save(update_fields=["file", "file_purged_at"])
        response = self.client.get(self.url)
        self.assertContains(response, "Il file originale non è più disponibile")
        self.assertNotContains(response, 'name="token"')
        # Il controllo POST resta: il file può sparire dopo il caricamento pagina.
        response = self.client.post(self.url, {"token": token}, follow=True)
        self.assertEqual(Job.objects.count(), 2)
        self.assertContains(response, "Il file originale non è più disponibile.")

    def test_ristampa_opzioni_non_più_valide(self):
        self.client.force_login(self.superuser)
        job, _ = services.create_job(
            self.w["key"],
            "produzione",
            SimpleUploadedFile("x.pdf", PDF),
            {"profile": "produzione", "options": {"carta": "a4-pesante"}},
        )
        profile = Profile.objects.get(tenant=self.w["tenant"], name="produzione")
        profile.options = {}
        profile.save()
        url = reverse("operations-reprint", args=[job.tenant_id, job.pk])
        token = re.search(
            r'name="token" value="([^"]+)"',
            self.client.get(url).content.decode(),
        ).group(1)
        response = self.client.post(url, {"token": token}, follow=True)
        self.assertEqual(Job.objects.count(), 2)
        self.assertContains(response, "opzione «carta»")

    def test_annullamento_finali_e_spooler(self):
        self.client.force_login(self.superuser)
        url = reverse("operations-cancel", args=[self.w["tenant"].pk, self.job.pk])
        Job.objects.filter(pk=self.job.pk).update(state=Job.State.COMPLETED)
        self.client.post(url)
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, Job.State.COMPLETED)

        Job.objects.filter(pk=self.job.pk).update(state=Job.State.SPOOLED)
        self.client.post(url)
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, Job.State.SPOOLED)
        self.assertTrue(self.job.events.filter(kind="cancel_requested").exists())
