from django.urls import path

from apps.clients import views

app_name = "clients"

urlpatterns = [
    path("", views.ClientListView.as_view(), name="client_list"),
    path("export/", views.ClientExportView.as_view(), name="client_export"),
    path("create/", views.ClientCreateView.as_view(), name="client_create"),
    path("<uuid:uuid>/", views.ClientDetailView.as_view(), name="client_detail"),
    path("<uuid:uuid>/edit/", views.ClientUpdateView.as_view(), name="client_edit"),
    path("<uuid:uuid>/deactivate/", views.client_deactivate, name="client_deactivate"),
    path("<uuid:uuid>/activate/", views.client_activate, name="client_activate"),
]
