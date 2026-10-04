from django.urls import path

from apps.alerts import views

app_name = "alerts"

urlpatterns = [
    path("", views.AlertListView.as_view(), name="alert_list"),
    path("report/", views.AlertReportView.as_view(), name="alert_report"),
    path("<uuid:uuid>/acknowledge/", views.acknowledge, name="alert_acknowledge"),
    path("<uuid:uuid>/resolve/", views.resolve, name="alert_resolve"),
    path("<uuid:uuid>/archive/", views.archive, name="alert_archive"),
]
