from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from hub.models import MaintenanceStatus
from hub.readiness import ready


class ReadinessTests(TestCase):
    def test_endpoint_non_espone_dettagli(self):
        with mock.patch("hub.readiness.ready", return_value=False):
            response = self.client.get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})
        with mock.patch("hub.readiness.ready", return_value=True):
            self.assertEqual(self.client.get("/readyz").status_code, 200)

    def test_timeout_probe(self):
        import subprocess

        with mock.patch(
            "hub.readiness.subprocess.run",
            side_effect=subprocess.TimeoutExpired("probe", 3),
        ):
            self.assertFalse(ready())

    def test_manutenzione_successo_e_fallimento(self):
        call_command("housekeeping")
        status = MaintenanceStatus.objects.get()
        success = status.last_success_at
        self.assertFalse(status.stale)
        self.assertFalse(status.failed)
        with mock.patch("hub.services.purge_files", side_effect=RuntimeError("errore")):
            with self.assertRaises(RuntimeError):
                call_command("housekeeping")
        status.refresh_from_db()
        self.assertTrue(status.failed)
        self.assertEqual(status.last_success_at, success)
        status.last_success_at = timezone.now() - timezone.timedelta(hours=4)
        self.assertTrue(status.stale)
