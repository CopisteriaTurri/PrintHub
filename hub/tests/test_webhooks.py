import hashlib
import hmac
import json
import ssl
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from hub import services, webhooks
from hub.models import Job, WebhookDelivery, WebhookDestination

from .helpers import PDF, TempMediaMixin, make_world


@override_settings(
    PRINTHUB_WEBHOOK_ALLOWED_HOSTS=["hook.example"], PRINTHUB_WEBHOOK_MAX_ATTEMPTS=2
)
class WebhookTests(TempMediaMixin, TestCase):
    def setUp(self):
        self.w = make_world()
        self.destination = WebhookDestination.objects.create(
            tenant=self.w["tenant"],
            name="hook",
            url="https://hook.example/path",
            secret_ref="PRINTHUB_WEBHOOK_SECRET_TEST",
        )
        self.job, _ = services.create_job(
            self.w["key"],
            "key",
            SimpleUploadedFile("a.pdf", PDF),
            {"profile": "fattura"},
        )

    def finish(self):
        services.update_job(self.w["agent"], self.job.pk, Job.State.COMPLETED)
        return WebhookDelivery.objects.get()

    def test_finale_transazionale_e_deduplicato(self):
        with self.assertRaises(ValueError), transaction.atomic():
            services.update_job(self.w["agent"], self.job.pk, Job.State.COMPLETED)
            raise ValueError()
        self.assertFalse(WebhookDelivery.objects.exists())
        item = self.finish()
        services.update_job(self.w["agent"], self.job.pk, Job.State.COMPLETED)
        self.assertEqual(WebhookDelivery.objects.count(), 1)
        self.assertEqual(item.payload["state"], "completed")
        self.assertEqual(item.payload["event_id"], str(item.event_id))

    def test_firma_sui_byte_inviati_e_ip_fissato(self):
        item = self.finish()
        response = mock.Mock(status=200)
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with (
            mock.patch.dict("os.environ", {"PRINTHUB_WEBHOOK_SECRET_TEST": "secret"}),
            mock.patch(
                "socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]
            ),
            mock.patch(
                "hub.webhooks.PinnedHTTPSConnection", return_value=connection
            ) as factory,
        ):
            self.assertEqual(webhooks.send(item), 0)
        factory.assert_called_once_with("hook.example", "8.8.8.8")
        kwargs = connection.request.call_args.kwargs
        body, headers = kwargs["body"], kwargs["headers"]
        self.assertEqual(json.loads(body), item.payload)
        expected = hmac.new(
            b"secret",
            headers["X-PrintHub-Timestamp"].encode() + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(headers["X-PrintHub-Signature"], "sha256=" + expected)

    def test_blocca_dns_privato_e_redirect(self):
        item = self.finish()
        with (
            mock.patch.dict("os.environ", {"PRINTHUB_WEBHOOK_SECRET_TEST": "secret"}),
            mock.patch(
                "socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]
            ),
            mock.patch("hub.webhooks.PinnedHTTPSConnection") as connection,
        ):
            self.assertEqual(webhooks.send(item), 1)
            connection.assert_not_called()
        response = mock.Mock(status=302)
        conn = mock.Mock()
        conn.getresponse.return_value = response
        with (
            mock.patch.dict("os.environ", {"PRINTHUB_WEBHOOK_SECRET_TEST": "secret"}),
            mock.patch(
                "socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]
            ),
            mock.patch("hub.webhooks.PinnedHTTPSConnection", return_value=conn),
        ):
            self.assertEqual(webhooks.send(item), 1)
            self.assertEqual(conn.request.call_count, 1)

    def test_lease_retry_e_retention_pendente(self):
        item = self.finish()
        item.event.timestamp = timezone.now() - timezone.timedelta(days=100)
        item.event.save()
        services.purge_events()
        self.assertTrue(WebhookDelivery.objects.filter(pk=item.pk).exists())
        with mock.patch("hub.webhooks.subprocess.run") as run:
            run.return_value.returncode = 75
            self.assertTrue(webhooks.deliver_once())
            item.refresh_from_db()
            self.assertEqual(item.attempts, 1)
            self.assertIsNone(item.failed_at)
            item.next_attempt_at = timezone.now()
            item.save()
            self.assertTrue(webhooks.deliver_once())
            item.refresh_from_db()
            self.assertIsNotNone(item.failed_at)

    def test_tutti_gli_stati_finali_e_isolamento(self):
        other = make_world("altro")
        WebhookDestination.objects.create(
            tenant=other["tenant"],
            name="altro",
            url="https://hook.example/",
            secret_ref="PRINTHUB_WEBHOOK_SECRET_TEST",
        )
        WebhookDestination.objects.create(
            tenant=self.w["tenant"],
            name="spento",
            enabled=False,
            url="https://hook.example/",
            secret_ref="PRINTHUB_WEBHOOK_SECRET_TEST",
        )
        for i, state in enumerate(
            [
                Job.State.COMPLETED,
                Job.State.FAILED,
                Job.State.CANCELED,
                Job.State.EXPIRED,
            ]
        ):
            job, _ = services.create_job(
                self.w["key"],
                f"final-{i}",
                SimpleUploadedFile("a.pdf", PDF),
                {"profile": "fattura"},
            )
            if state == Job.State.CANCELED:
                services.cancel_job(self.w["key"], job.pk)
            elif state == Job.State.EXPIRED:
                Job.objects.filter(pk=job.pk).update(
                    expires_at=timezone.now() - timezone.timedelta(seconds=1)
                )
                services.get_job(self.w["key"], job.pk)
            else:
                services.update_job(self.w["agent"], job.pk, state)
            delivery = WebhookDelivery.objects.get(event__job=job)
            self.assertEqual(delivery.payload["state"], state)
            self.assertEqual(delivery.destination_id, self.destination.pk)

    def test_rifiuta_dns_misto_ipv6_e_http(self):
        item = self.finish()
        addresses = [
            (2, 1, 6, "", ("8.8.8.8", 443)),
            (10, 1, 6, "", ("::1", 443, 0, 0)),
        ]
        with (
            mock.patch.dict("os.environ", {"PRINTHUB_WEBHOOK_SECRET_TEST": "secret"}),
            mock.patch("socket.getaddrinfo", return_value=addresses),
            mock.patch("hub.webhooks.PinnedHTTPSConnection") as connection,
        ):
            self.assertEqual(webhooks.send(item), 1)
            connection.assert_not_called()
        with self.assertRaises(ValueError):
            webhooks.validate_destination(
                "http://hook.example/", "PRINTHUB_WEBHOOK_SECRET_TEST"
            )

    def test_risposte_http_e_lease_scaduto(self):
        item = self.finish()
        for status, expected in [(200, 0), (429, 75), (503, 75), (400, 1)]:
            conn = mock.Mock()
            conn.getresponse.return_value.status = status
            with (
                mock.patch.dict(
                    "os.environ", {"PRINTHUB_WEBHOOK_SECRET_TEST": "secret"}
                ),
                mock.patch(
                    "socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]
                ),
                mock.patch("hub.webhooks.PinnedHTTPSConnection", return_value=conn),
            ):
                self.assertEqual(webhooks.send(item), expected)
        first = webhooks.claim()
        self.assertIsNotNone(first)
        self.assertIsNone(webhooks.claim())
        WebhookDelivery.objects.filter(pk=item.pk).update(
            lease_until=timezone.now() - timezone.timedelta(seconds=1)
        )
        second = webhooks.claim()
        self.assertNotEqual(first[1], second[1])
        self.assertEqual(
            WebhookDelivery.objects.filter(pk=item.pk, lease_token=first[1]).update(
                result="vecchio"
            ),
            0,
        )
        WebhookDelivery.objects.filter(pk=item.pk).update(
            lease_until=timezone.now() - timezone.timedelta(seconds=1)
        )
        with mock.patch("hub.webhooks.subprocess.run") as run:
            run.return_value.returncode = 0
            self.assertTrue(webhooks.deliver_once())
        item.refresh_from_db()
        self.assertIsNotNone(item.completed_at)
        self.assertEqual(item.attempt_log.count(), 1)

    def test_segreto_mancante_temporaneo_e_certificato_permanente(self):
        item = self.finish()
        with mock.patch.dict("os.environ", {"PRINTHUB_WEBHOOK_SECRET_TEST": ""}):
            self.assertEqual(webhooks.send(item), webhooks.EXIT_TEMPORARY)
        connection = mock.Mock()
        connection.request.side_effect = ssl.SSLCertVerificationError("certificato")
        with (
            mock.patch.dict("os.environ", {"PRINTHUB_WEBHOOK_SECRET_TEST": "secret"}),
            mock.patch(
                "socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]
            ),
            mock.patch("hub.webhooks.PinnedHTTPSConnection", return_value=connection),
        ):
            self.assertEqual(webhooks.send(item), 1)

    def test_uscita_anomala_del_figlio_e_temporanea(self):
        item = self.finish()
        with mock.patch("hub.webhooks.subprocess.run") as run:
            run.return_value.returncode = 1  # il figlio non è partito
            self.assertTrue(webhooks.deliver_once())
        item.refresh_from_db()
        self.assertIsNone(item.failed_at)
        self.assertEqual(item.result, "errore temporaneo")
        item.next_attempt_at = timezone.now()
        item.save(update_fields=["next_attempt_at"])
        with mock.patch("hub.webhooks.subprocess.run") as run:
            run.return_value.returncode = webhooks.EXIT_PERMANENT
            self.assertTrue(webhooks.deliver_once())
        item.refresh_from_db()
        self.assertIsNotNone(item.failed_at)
        self.assertEqual(item.result, "errore permanente")

    def test_comando_figlio_segnala_il_permanente(self):
        item = self.finish()
        WebhookDelivery.objects.filter(pk=item.pk).update(
            lease_token="token",
            lease_until=timezone.now() + timezone.timedelta(seconds=60),
        )
        with mock.patch("hub.management.commands.send_webhook.send", return_value=1):
            with self.assertRaises(SystemExit) as exit_:
                call_command("send_webhook", item.pk, "token")
        self.assertEqual(exit_.exception.code, webhooks.EXIT_PERMANENT)
