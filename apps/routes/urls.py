from django.urls import path

from apps.routes import views

app_name = "routes"

urlpatterns = [
    path("", views.RouteListView.as_view(), name="route_list"),
    path("export/", views.RouteExportView.as_view(), name="route_export"),
    path("create/", views.RouteCreateView.as_view(), name="route_create"),
    path("<uuid:uuid>/", views.RouteDetailView.as_view(), name="route_detail"),
    path("<uuid:uuid>/edit/", views.RouteUpdateView.as_view(), name="route_edit"),
    path("<uuid:uuid>/deactivate/", views.route_deactivate, name="route_deactivate"),
    path("<uuid:uuid>/activate/", views.route_activate, name="route_activate"),
]
