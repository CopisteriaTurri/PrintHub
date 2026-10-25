import os
import subprocess
from unittest import mock

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings

from hub.version import get_version


class VersioneTests(SimpleTestCase):
    """La versione: build-arg in produzione, git in sviluppo."""

    def test_env_vince_su_git(self):
        # In produzione è l'unica fonte: nell'immagine git non c'è.
        with (
            mock.patch.dict(os.environ, {"APP_VERSION": "abc1234"}),
            mock.patch("hub.version.subprocess.check_output") as git,
        ):
            self.assertEqual(get_version(), "abc1234")
        git.assert_not_called()

    def test_env_vuota_ripiega_su_git(self):
        # Il Dockerfile dichiara `ARG APP_VERSION=""`: una build senza
        # build-arg arriva qui con la variabile definita ma vuota.
        with (
            mock.patch.dict(os.environ, {"APP_VERSION": ""}),
            mock.patch(
                "hub.version.subprocess.check_output", return_value="v1.2-3-gdeadbee\n"
            ),
        ):
            self.assertEqual(get_version(), "v1.2-3-gdeadbee")

    def test_senza_env_ne_git_stringa_vuota(self):
        for errore in (
            OSError("git assente"),
            subprocess.CalledProcessError(128, "git"),
        ):
            with (
                self.subTest(errore=type(errore).__name__),
                mock.patch.dict(os.environ, {"APP_VERSION": ""}),
                mock.patch("hub.version.subprocess.check_output", side_effect=errore),
            ):
                self.assertEqual(get_version(), "")


# Senza collectstatic non c'è il manifest che lo storage di produzione richiede.
@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    }
)
class VersioneNellAdminTests(TestCase):
    def test_header_dell_admin(self):
        user = User.objects.create_superuser("admin", "a@example.com", "x")
        self.client.force_login(user)
        with mock.patch("hub.version.APP_VERSION", "abc1234"):
            response = self.client.get("/admin/")
        self.assertContains(response, "vabc1234")
