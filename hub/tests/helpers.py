"""Dati e strumenti comuni ai test."""

import shutil
import tempfile

from django.test import override_settings

from hub import services
from hub.models import Agent, ApiKey, Tenant

PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32

PROFILES = [
    {
        "name": "fattura",
        "description": "A4 B/N",
        "printer": "HP LaserJet Pro",
        "accepts": ["application/pdf"],
        "options": {},
    },
    {
        "name": "produzione",
        "printer": "Konica bizhub PRESS",
        "accepts": ["application/pdf"],
        "options": {
            "carta": {
                "label": "Carta",
                "default": "a4-normale",
                "values": [
                    {"value": "a4-normale", "label": "A4 80 g"},
                    {"value": "a4-pesante", "label": "A4 250 g"},
                ],
            }
        },
    },
    {
        "name": "etichetta",
        "printer": "DYMO",
        "accepts": ["image/png"],
    },
]


class TempMediaMixin:
    """Ogni classe di test scrive i file in una cartella temporanea propria."""

    @classmethod
    def setUpClass(cls):
        cls._media = tempfile.mkdtemp(prefix="printhub-test-")
        cls._media_override = override_settings(MEDIA_ROOT=cls._media)
        cls._media_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._media_override.disable()
        shutil.rmtree(cls._media, ignore_errors=True)


def make_world(tenant_slug="copisteria"):
    """Un tenant con un agente che offre PROFILES e una API key senza limiti."""
    tenant = Tenant.objects.create(slug=tenant_slug, name=tenant_slug.title())
    agent, agent_token = Agent.create(tenant, "pc-banco")
    services.sync_profiles(agent, PROFILES)
    key, key_value = ApiKey.create(tenant, "CopyaGo")
    return {
        "tenant": tenant,
        "agent": agent,
        "agent_token": agent_token,
        "key": key,
        "key_value": key_value,
    }
