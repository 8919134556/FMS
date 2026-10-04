from django.urls import path

from apps.maintenance import views

app_name = "maintenance"

urlpatterns = [
    path("schedule/", views.ServiceScheduleView.as_view(), name="service_schedule"),
    path("", views.MaintenanceListView.as_view(), name="maintenance_list"),
    path("export/", views.MaintenanceExportView.as_view(), name="maintenance_export"),
    path("create/", views.MaintenanceCreateView.as_view(), name="maintenance_create"),
    path("<uuid:uuid>/", views.MaintenanceDetailView.as_view(), name="maintenance_detail"),
    path("<uuid:uuid>/edit/", views.MaintenanceUpdateView.as_view(), name="maintenance_edit"),
    path("<uuid:uuid>/start/", views.maintenance_start, name="maintenance_start"),
    path("<uuid:uuid>/complete/", views.maintenance_complete, name="maintenance_complete"),
    path("<uuid:uuid>/cancel/", views.maintenance_cancel, name="maintenance_cancel"),
]
