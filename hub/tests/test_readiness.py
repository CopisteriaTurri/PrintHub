from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from hub import readiness
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

        readiness._cached_at = 0
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

    def test_cache_e_prove_concorrenti(self):
        readiness._cached_at = 0
        with mock.patch("hub.readiness._probe", return_value=True) as probe:
            self.assertTrue(ready())
            self.assertTrue(ready())
            probe.assert_called_once()
        readiness._cached_at = 0
        with readiness._lock:
            self.assertFalse(ready())

    def test_comando_probe_pulisce_file_e_segnala_guasti(self):
        from io import BytesIO

        from hub.management.commands import check_readiness

        storage = mock.MagicMock()
        storage.save.return_value = "probe"
        storage.open.return_value.__enter__.return_value = BytesIO(b"ok")
        layer = mock.Mock()

        async def new_channel(*args):
            return "channel"

        async def send(*args):
            return None

        async def receive(*args):
            return {"type": "readiness.probe"}

        layer.new_channel = new_channel
        layer.send = send
        layer.receive = receive
        command = check_readiness.Command()
        with (
            mock.patch.object(check_readiness, "default_storage", storage),
            mock.patch.object(check_readiness, "get_channel_layer", return_value=layer),
        ):
            command.handle()
            storage.delete.assert_called_with("probe")
            storage.open.side_effect = OSError("guasto")
            with self.assertRaises(OSError):
                command.handle()
            self.assertEqual(storage.delete.call_count, 2)
            storage.open.side_effect = None
            with mock.patch.object(
                check_readiness, "get_channel_layer", return_value=None
            ):
                with self.assertRaises(RuntimeError):
                    command.handle()
            with mock.patch.object(
                check_readiness.connection, "cursor", side_effect=OSError("db")
            ):
                with self.assertRaises(OSError):
                    command.handle()
