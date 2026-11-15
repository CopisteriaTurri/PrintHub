"""Revoca delle credenziali dall'admin: azione «Revoca» e scheda dell'agente."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hub.models import Agent, ApiKey

from .helpers import make_world
from .test_operations import STORAGE_SENZA_MANIFEST


@override_settings(STORAGES=STORAGE_SENZA_MANIFEST)
class RevocaAdminTests(TestCase):
    def setUp(self):
        self.w = make_world()
        self.agent = self.w["agent"]
        self.client.force_login(
            get_user_model().objects.create_superuser("admin", "a@example.invalid", "x")
        )
        self.url = reverse("admin:hub_agent_change", args=[self.agent.pk])

    def form_data(self):
        """I dati della scheda così come li rimanderebbe il browser."""
        context = self.client.get(self.url).context
        forms = [context["adminform"].form]
        for inline in context["inline_admin_formsets"]:
            forms += [inline.formset.management_form, *inline.formset.forms]
        data = {}
        for form in forms:
            for bound in form:
                value = bound.value()
                if value is not None:
                    data[bound.html_name] = value
        return data

    def save(self, **changes):
        with mock.patch("hub.notify.agent_close") as closed:
            with self.captureOnCommitCallbacks(execute=True):
                r = self.client.post(self.url, {**self.form_data(), **changes})
        self.assertEqual(r.status_code, 302, r.content[:2000])
        self.agent.refresh_from_db()
        return closed

    def test_revoca_dalla_scheda_chiude_la_connessione(self):
        closed = self.save(revoked_at_0="2026-01-02", revoked_at_1="03:04:05")
        self.assertIsNotNone(self.agent.revoked_at)
        closed.assert_called_once_with(self.agent.id, "revoked")

    def test_salvare_la_scheda_senza_revocare_non_chiude(self):
        closed = self.save(name="pc-cassa")
        self.assertEqual(self.agent.name, "pc-cassa")
        self.assertIsNone(self.agent.revoked_at)
        closed.assert_not_called()

    def test_togliere_la_revoca_dalla_scheda_non_chiude(self):
        Agent.objects.filter(pk=self.agent.pk).update(revoked_at=timezone.now())
        closed = self.save(revoked_at_0="", revoked_at_1="")
        self.assertIsNone(self.agent.revoked_at)
        closed.assert_not_called()

    def test_azione_revoca_chiude_la_connessione(self):
        with mock.patch("hub.notify.agent_close") as closed:
            with self.captureOnCommitCallbacks(execute=True):
                r = self.client.post(
                    reverse("admin:hub_agent_changelist"),
                    {"action": "revoke", "_selected_action": [self.agent.pk]},
                )
        self.assertEqual(r.status_code, 302)
        self.agent.refresh_from_db()
        self.assertIsNotNone(self.agent.revoked_at)
        closed.assert_called_once_with(self.agent.id, "revoked")

    def test_azione_revoca_sulle_api_key(self):
        with mock.patch("hub.notify.agent_close") as closed:
            r = self.client.post(
                reverse("admin:hub_apikey_changelist"),
                {"action": "revoke", "_selected_action": [self.w["key"].pk]},
            )
        self.assertEqual(r.status_code, 302)
        self.assertIsNotNone(ApiKey.objects.get(pk=self.w["key"].pk).revoked_at)
        closed.assert_not_called()
