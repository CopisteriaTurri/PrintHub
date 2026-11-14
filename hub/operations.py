"""Operazioni admin riservate ai superuser."""

import uuid
from datetime import timedelta
from functools import wraps

from django.contrib import admin, messages
from django.core import signing
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.paginator import Paginator
from django.db import transaction
from django.http import HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import services
from .models import Agent, Job, MaintenanceStatus, Tenant

# Finestra degli errori mostrati nel riepilogo: `updated_at` è l'ultima
# transizione di stato, non esiste un campo dedicato alla chiusura.
ERROR_WINDOW = timedelta(hours=24)


def superuser(view):
    @admin.site.admin_view
    @wraps(view)
    def protected(request, *args, **kwargs):
        if not request.user.is_superuser:
            return HttpResponseForbidden()
        return view(request, *args, **kwargs)

    return protected


@superuser
@require_GET
def dashboard(request):
    jobs = Job.objects.select_related("tenant", "agent", "api_key")
    tenant = request.GET.get("tenant", "")
    agent = request.GET.get("agent", "")
    state = request.GET.get("state", "")
    if tenant:
        if not tenant.isdecimal() or not Tenant.objects.filter(pk=tenant).exists():
            return HttpResponseBadRequest("Tenant non valido")
        jobs = jobs.filter(tenant_id=tenant)
    if agent:
        if not agent.isdecimal() or not Agent.objects.filter(pk=agent).exists():
            return HttpResponseBadRequest("Agente non valido")
        jobs = jobs.filter(agent_id=agent)
    if state:
        if state not in Job.State.values:
            return HttpResponseBadRequest("Stato non valido")
        jobs = jobs.filter(state=state)
    # La presenza segue gli stessi filtri dei lavori; le opzioni dei select
    # restano su tutti gli agenti, altrimenti dopo un filtro non si torna indietro.
    agents = Agent.objects.select_related("tenant")
    if tenant:
        agents = agents.filter(tenant_id=tenant)
    if agent:
        agents = agents.filter(pk=agent)
    context = {
        **admin.site.each_context(request),
        "title": "Operatività",
        "jobs": Paginator(jobs, 30).get_page(request.GET.get("page")),
        "tenants": Tenant.objects.all(),
        "agents": agents,
        "agent_choices": Agent.objects.select_related("tenant"),
        "states": Job.State.choices,
        "queued": jobs.filter(state=Job.State.QUEUED).count(),
        "stalled": jobs.filter(Job.stalled_filter()).count(),
        "errors": jobs.filter(
            state=Job.State.FAILED,
            updated_at__gte=timezone.now() - ERROR_WINDOW,
        ).count(),
        "errors_hours": int(ERROR_WINDOW.total_seconds() // 3600),
        "maintenance": MaintenanceStatus.objects.first(),
        "selected_tenant": tenant,
        "selected_agent": agent,
        "selected_state": state,
    }
    return render(request, "admin/hub/operations.html", context)


@superuser
@require_POST
def cancel(request, tenant_id, job_id):
    job = get_object_or_404(
        Job.objects.select_related("api_key"), tenant_id=tenant_id, pk=job_id
    )
    try:
        services.cancel_job(job.api_key, job.id)
        messages.success(request, "Richiesta di annullamento registrata.")
    except services.HubError as exc:
        messages.error(request, exc.message)
    return redirect("operations")


@superuser
@require_http_methods(["GET", "POST"])
def reprint(request, tenant_id, job_id):
    job = get_object_or_404(
        Job.objects.select_related("tenant", "api_key"), tenant_id=tenant_id, pk=job_id
    )
    if request.method == "GET":
        token = signing.dumps(
            {"job": job.id, "tenant": tenant_id, "nonce": uuid.uuid4().hex},
            salt="reprint",
        )
        return render(
            request,
            "admin/hub/reprint.html",
            {
                **admin.site.each_context(request),
                "title": "Conferma ristampa",
                "job": job,
                "token": token,
            },
        )
    try:
        data = signing.loads(
            request.POST.get("token", ""), salt="reprint", max_age=86400
        )
        if data.get("job") != job.id or data.get("tenant") != tenant_id:
            raise signing.BadSignature()
    except signing.BadSignature:
        return HttpResponseBadRequest("Conferma non valida o scaduta")
    try:
        if job.api_key.revoked_at:
            raise services.HubError("revoked", "La API key originale è revocata.")
        if job.file_purged_at or not job.file:
            raise services.HubError(
                "missing_file", "Il file originale non è più disponibile."
            )
        try:
            with job.file.open("rb") as source:
                contents = source.read()
        except OSError:
            raise services.HubError(
                "missing_file", "Il file originale non è disponibile sul disco."
            ) from None
        meta = {
            "profile": job.profile_name,
            "options": job.options,
            "copies": job.copies,
            "title": job.title,
            "client_ref": job.client_ref,
            "reprint_of": job.id,
        }
        with transaction.atomic():
            new_job, created = services.create_job(
                job.api_key,
                "reprint-" + data["nonce"],
                SimpleUploadedFile("reprint", contents),
                meta,
                source="admin",
            )
            if created:
                services.record_event(
                    new_job,
                    "reprint_requested",
                    "admin",
                    {"user_id": request.user.pk, "original_job": job.id},
                )
        messages.success(
            request,
            f"Ristampa {new_job.id} {'creata' if created else 'già richiesta'}.",
        )
        return redirect("admin:hub_job_change", new_job.pk)
    except services.HubError as exc:
        messages.error(request, exc.message)
        return redirect("operations")
