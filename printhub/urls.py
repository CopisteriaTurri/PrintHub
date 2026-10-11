from django.contrib import admin
from django.urls import path

from hub import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/profiles", views.profiles, name="api-profiles"),
    path("api/v1/jobs", views.jobs, name="api-jobs"),
    path("api/v1/jobs/<str:job_id>", views.job_detail, name="api-job"),
    path("api/v1/jobs/<str:job_id>/cancel", views.job_cancel, name="api-job-cancel"),
    path(
        "agent/v1/jobs/<str:job_id>/file", views.agent_job_file, name="agent-job-file"
    ),
    path("healthz", views.healthz, name="healthz"),
]
