import re

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hub import services
from hub.models import Job

from .helpers import PDF, TempMediaMixin, make_world


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

    def test_permessi_filtri_e_get_senza_effetti(self):
        self.assertEqual(self.client.get(reverse("operations")).status_code, 302)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("operations")).status_code, 403)
        self.client.force_login(self.superuser)
        self.assertEqual(
            self.client.get(reverse("operations"), {"tenant": "x"}).status_code, 400
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

    def test_ristampa_idempotente_file_indipendente_e_key_revocata(self):
        self.client.force_login(self.superuser)
        response = self.client.get(self.url)
        token = re.search(
            r'name="token" value="([^"]+)"', response.content.decode()
        ).group(1)
        self.client.post(self.url, {"token": token})
        self.client.post(self.url, {"token": token})
        self.assertEqual(Job.objects.count(), 2)
        new = Job.objects.exclude(pk=self.job.pk).get()
        self.assertEqual(new.reprint_of_id, self.job.pk)
        self.assertNotEqual(new.file.name, self.job.file.name)
        self.assertTrue(new.events.filter(kind="reprint_requested").exists())
        self.w["key"].revoked_at = timezone.now()
        self.w["key"].save()
        token2 = re.search(
            r'name="token" value="([^"]+)"', self.client.get(self.url).content.decode()
        ).group(1)
        self.client.post(self.url, {"token": token2})
        self.assertEqual(Job.objects.count(), 2)
