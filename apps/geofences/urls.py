from django.urls import path

from apps.geofences import api_views, views

app_name = "geofences"

urlpatterns = [
    path("", views.GeofenceListView.as_view(), name="geofence_list"),
    path("map-data/", api_views.GeofenceMapDataView.as_view(), name="geofence_map_data"),
    path("export/", views.GeofenceExportView.as_view(), name="geofence_export"),
    path("create/", views.GeofenceCreateView.as_view(), name="geofence_create"),
    path("<uuid:uuid>/", views.GeofenceDetailView.as_view(), name="geofence_detail"),
    path("<uuid:uuid>/edit/", views.GeofenceUpdateView.as_view(), name="geofence_edit"),
    path("<uuid:uuid>/deactivate/", views.geofence_deactivate, name="geofence_deactivate"),
    path("<uuid:uuid>/activate/", views.geofence_activate, name="geofence_activate"),
]
