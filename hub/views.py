"""View HTTP: API dei client (PROTOCOL.md §3) e download dei file per gli agenti (§5.2)."""

import functools
import json

from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import services
from .services import HubError
from .version import APP_VERSION


def error_response(code, message, status):
    return JsonResponse({"error": {"code": code, "message": message}}, status=status)


def client_api(view):
    """Autentica la API key e traduce gli HubError in risposte JSON.

    Niente CSRF: l'API non usa cookie ma un token nell'header, che un sito
    terzo non può far inviare al browser.
    """

    @csrf_exempt
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        key = services.authenticate_api_key(
            services.bearer(request.headers.get("Authorization"))
        )
        if key is None:
            return error_response(
                "unauthorized", "API key mancante, non valida o revocata", 401
            )
        try:
            return view(request, key, *args, **kwargs)
        except HubError as e:
            return error_response(e.code, e.message, e.status)

    return wrapper


@require_GET
@client_api
def profiles(request, key):
    return JsonResponse(
        {"profiles": [services.profile_to_dict(p) for p in services.profiles_for(key)]}
    )


@require_http_methods(["GET", "POST"])
@client_api
def jobs(request, key):
    if request.method == "GET":
        client_ref = request.GET.get("client_ref", "")
        if not client_ref:
            raise HubError("invalid_request", "serve il parametro client_ref")
        found = services.jobs_by_client_ref(key, client_ref)
        return JsonResponse({"jobs": [services.job_to_dict(j) for j in found]})

    upload = request.FILES.get("file")
    if upload is None:
        raise HubError("invalid_request", "manca la parte «file»")
    try:
        meta = json.loads(request.POST.get("meta") or "")
    except json.JSONDecodeError:
        raise HubError("invalid_request", "la parte «meta» non è JSON valido") from None
    job, created = services.create_job(
        key, request.headers.get("Idempotency-Key", ""), upload, meta
    )
    return JsonResponse(services.job_to_dict(job), status=201 if created else 200)


@require_GET
@client_api
def job_detail(request, key, job_id):
    return JsonResponse(services.job_to_dict(services.get_job(key, job_id)))


@require_POST
@client_api
def job_cancel(request, key, job_id):
    return JsonResponse(services.job_to_dict(services.cancel_job(key, job_id)))


@require_GET
def agent_job_file(request, job_id):
    agent = services.authenticate_agent(
        services.bearer(request.headers.get("Authorization"))
    )
    if agent is None:
        return error_response(
            "unauthorized", "token dell'agente mancante, non valido o revocato", 401
        )
    job = services.can_download(agent, job_id)
    if job is None:
        # Stessa risposta per "non esiste" e "non è tuo": non si rivela niente.
        return error_response("not_found", "file non disponibile", 404)
    # Risposta normale e non FileResponse: sotto ASGI Django leggerebbe comunque
    # tutto il file in memoria (e lo segnala con un warning). I file sono limitati
    # da PRINTHUB_MAX_FILE_SIZE.
    with job.file.open("rb") as f:
        return HttpResponse(f.read(), content_type=job.mime)


@require_GET
def healthz(request):
    return JsonResponse({"status": "ok", "version": APP_VERSION})
