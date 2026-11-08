"""Admin: consultazione di agenti, profili e lavori, revoca delle credenziali.

API key e token degli agenti si creano con i comandi `create_api_key` e
`create_agent`, che mostrano il valore in chiaro una sola volta.
"""

from django.contrib import admin
from django.utils import timezone

from . import notify
from .models import Agent, ApiKey, Job, Profile, Tenant


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
