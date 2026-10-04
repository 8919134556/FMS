from django.urls import path

from apps.core.views import DashboardView, MasterDataView, SettingsView

app_name = "core"

urlpatterns = [
    path("", DashboardView.as_view(), name="dashboard"),
    path("master-data/", MasterDataView.as_view(), name="master_data"),
    path("settings/", SettingsView.as_view(), name="settings"),
]
