"""Impostazioni di Print Hub.

Tutto ciò che cambia tra sviluppo e produzione arriva da variabili d'ambiente
(vedi .env.example). I default sono pensati per lo sviluppo locale: SQLite e
canali in memoria, senza Valkey.
"""

import os
import sys
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes")


def env_int(name, default):
    return int(os.environ.get(name, default))


DEBUG = env_bool("DJANGO_DEBUG", False)
TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"

SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    if not (DEBUG or TESTING):
        raise RuntimeError("SECRET_KEY è obbligatoria con DJANGO_DEBUG=False")
    SECRET_KEY = "solo-sviluppo-non-usare-in-produzione"

ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",")
    if h.strip()
]
if DEBUG:
    ALLOWED_HOSTS += ["localhost", "127.0.0.1", "[::1]", "testserver"]

CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
    if o.strip()
]

# Dietro cloudflared/reverse proxy il TLS termina prima di Django.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
# Redirect a HTTPS e HSTS li fa cloudflared davanti all'hub. Farli anche qui
# romperebbe gli accessi diretti in LAN e i controlli di salute su /healthz.
SILENCED_SYSTEM_CHECKS = ["security.W004", "security.W008"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "channels",
    "hub",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "printhub.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

ASGI_APPLICATION = "printhub.asgi.application"

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}", conn_max_age=60
    )
}

# Canali: Valkey in produzione (più processi devono potersi parlare: la richiesta
# HTTP che crea un lavoro avvisa la connessione WebSocket dell'agente, che può
# stare in un altro processo). In memoria va bene solo con un processo, cioè in
# sviluppo e nei test.
# Valkey parla il protocollo di Redis: l'URL è `redis://…` e il backend è
# quello di channels-redis. Va bene anche Redis.
CHANNEL_LAYER_URL = os.environ.get("CHANNEL_LAYER_URL", "")
if CHANNEL_LAYER_URL:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [CHANNEL_LAYER_URL]},
        }
    }
else:
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "it"
TIME_ZONE = "Europe/Rome"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

# I file dei lavori non sono pubblici: si scaricano solo dall'endpoint
# autenticato dell'agente, mai da un URL statico.
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", BASE_DIR / "media"))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Print Hub (docs/PROTOCOL.md) ---

# Dimensione massima di un file da stampare.
PRINTHUB_MAX_FILE_SIZE = env_int("PRINTHUB_MAX_FILE_SIZE", 50 * 1024 * 1024)
# Scadenza di default di un lavoro non partito (§3.2), e limiti ammessi.
PRINTHUB_DEFAULT_EXPIRES_IN = env_int("PRINTHUB_DEFAULT_EXPIRES_IN", 600)
PRINTHUB_MAX_EXPIRES_IN = env_int("PRINTHUB_MAX_EXPIRES_IN", 24 * 3600)
# Giorni dopo cui si cancellano i file dei lavori (dati personali, §8).
PRINTHUB_FILE_RETENTION_DAYS = env_int("PRINTHUB_FILE_RETENTION_DAYS", 7)
# Intervallo di heartbeat comunicato agli agenti (§5.4).
PRINTHUB_HEARTBEAT_INTERVAL = env_int("PRINTHUB_HEARTBEAT_INTERVAL", 20)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
