"""Comandi di gestione: creazione delle credenziali e manutenzione."""

from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase

from hub import services
from hub.models import Agent, ApiKey, Tenant


def run(*args):
    out = StringIO()
    call_command(*args, stdout=out)
    return out.getvalue().strip().splitlines()


class CommandsTests(TestCase):
    def setUp(self):
        run("create_tenant", "copisteria", "Copisteria Turri")

    def test_api_key_mostrata_una_volta_e_funzionante(self):
        lines = run("create_api_key", "copisteria", "CopyaGo", "--profile", "fattura")
        value = lines[-1]
        self.assertTrue(value.startswith("phk_"))
        key = services.authenticate_api_key(value)
        self.assertEqual(key.allowed_profiles, ["fattura"])
        # Nel database c'è solo l'hash.
        self.assertFalse(ApiKey.objects.filter(key_hash=value).exists())

    def test_agente(self):
        value = run("create_agent", "copisteria", "pc-banco")[-1]
        self.assertTrue(value.startswith("pha_"))
        self.assertEqual(services.authenticate_agent(value), Agent.objects.get())

    def test_tenant_inesistente(self):
        with self.assertRaises(CommandError):
            run("create_agent", "nessuno", "pc")

    def test_housekeeping(self):
        self.assertEqual(
            run("housekeeping"), ["Lavori scaduti: 0. File cancellati: 0."]
        )
        self.assertEqual(Tenant.objects.count(), 1)


class WorkersGuardTests(TestCase):
    def test_piu_processi_senza_channel_layer_non_parte(self):
        import os
        import subprocess
        import sys

        env = {**os.environ, "WEB_CONCURRENCY": "2", "DJANGO_DEBUG": "1"}
        env.pop("CHANNEL_LAYER_URL", None)
        result = subprocess.run(
            [sys.executable, "manage.py", "check"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("richiede CHANNEL_LAYER_URL", result.stderr)
