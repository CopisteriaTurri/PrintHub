"""Admin: consultazione di agenti, profili e lavori, revoca delle credenziali.

API key e token degli agenti si creano con i comandi `create_api_key` e
`create_agent`, che mostrano il valore in chiaro una sola volta.
"""

from django.contrib import admin
from django.utils import timezone

from . import notify
from .models import (
    Agent,
    ApiKey,
    Job,
    JobEvent,
    MaintenanceStatus,
    Profile,
    Tenant,
    WebhookAttempt,
    WebhookDelivery,
    WebhookDestination,
)


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "max_copies", "created_at"]
    prepopulated_fields = {"slug": ["name"]}


@admin.action(description="Revoca le credenziali selezionate")
def revoke(modeladmin, request, queryset):
    now = timezone.now()
    for obj in queryset.filter(revoked_at__isnull=True):
        obj.revoked_at = now
        obj.save(update_fields=["revoked_at"])
        if isinstance(obj, Agent):
            notify.agent_close(obj.id, "revoked")


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "tenant",
        "hint",
        "allowed_profiles",
        "last_used_at",
        "revoked_at",
    ]
    list_filter = ["tenant"]
    readonly_fields = ["hint", "created_at", "last_used_at"]
    actions = [revoke]

    def has_add_permission(self, request):
        return False  # si creano con `manage.py create_api_key`


class ProfileInline(admin.TabularInline):
    model = Profile
    fields = ["name", "printer", "description", "accepts", "options"]
    readonly_fields = fields
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False  # li dichiara l'agente


@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "tenant",
        "is_online",
        "agent_version",
        "os",
        "last_seen_at",
        "revoked_at",
    ]
    list_filter = ["tenant"]
    readonly_fields = [
        "hint",
        "created_at",
        "connected_at",
        "last_seen_at",
        "agent_version",
        "os",
        "printers",
    ]
    inlines = [ProfileInline]
    actions = [revoke]

    @admin.display(boolean=True, description="online")
    def is_online(self, obj):
        return obj.online

    def has_add_permission(self, request):
        return False  # si creano con `manage.py create_agent`


class StalledFilter(admin.SimpleListFilter):
    title = "potenzialmente bloccato"
    parameter_name = "stalled"

    def lookups(self, request, model_admin):
        return [("yes", "Sì"), ("no", "No")]

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(Job.stalled_filter())
        if self.value() == "no":
            return queryset.exclude(Job.stalled_filter())
        return queryset


class JobEventInline(admin.TabularInline):
    model = JobEvent
    fields = ["timestamp", "kind", "source", "details"]
    readonly_fields = fields
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "state",
        "stalled",
        "agent_seen_at",
        "elapsed",
        "profile_name",
        "agent",
        "title",
        "copies",
        "api_key",
        "created_at",
    ]
    list_filter = [StalledFilter, "state", "tenant", "agent", "api_key"]
    list_select_related = ["agent", "api_key", "tenant"]
    inlines = [JobEventInline]
    search_fields = ["id", "title", "client_ref", "idempotency_key"]
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in Job._meta.fields]

    @admin.display(boolean=True, description="potenzialmente bloccato")
    def stalled(self, obj):
        return obj.is_stalled

    @admin.display(description="tempo dall’ultimo riscontro")
    def elapsed(self, obj):
        return timezone.now() - (obj.agent_seen_at or obj.created_at)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False  # lo storico non si modifica a mano


@admin.register(MaintenanceStatus)
class MaintenanceStatusAdmin(admin.ModelAdmin):
    list_display = ["last_attempt_at", "last_success_at", "failed", "is_stale"]
    readonly_fields = [f.name for f in MaintenanceStatus._meta.fields]

    @admin.display(boolean=True, description="manutenzione in ritardo")
    def is_stale(self, obj):
        return obj.stale

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class SuperuserOnlyAdmin(admin.ModelAdmin):
    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(WebhookDestination)
class WebhookDestinationAdmin(SuperuserOnlyAdmin):
    list_display = ["name", "tenant", "api_key", "enabled"]

    def get_readonly_fields(self, request, obj=None):
        # Le consegne in coda puntano alla destinazione: cambiarne tenant, key o
        # URL dopo la creazione manderebbe payload già accodati altrove. Per un
        # altro destinatario si crea una destinazione nuova.
        if obj is not None:
            return ["tenant", "api_key", "url"]
        return []


class WebhookAttemptInline(admin.TabularInline):
    model = WebhookAttempt
    fields = ["timestamp", "result"]
    readonly_fields = fields
    can_delete = False
    extra = 0

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(WebhookDelivery)
class WebhookDeliveryAdmin(SuperuserOnlyAdmin):
    list_display = ["event", "destination", "attempts", "result", "next_attempt_at"]
    inlines = [WebhookAttemptInline]
    readonly_fields = [f.name for f in WebhookDelivery._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
