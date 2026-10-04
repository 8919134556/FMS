from django.urls import path

from apps.drivers import views

app_name = "drivers"

urlpatterns = [
    path("drivers/", views.DriverListView.as_view(), name="driver_list"),
    path("drivers/export/", views.DriverExportView.as_view(), name="driver_export"),
    path("drivers/create/", views.DriverCreateView.as_view(), name="driver_create"),
    path("drivers/<uuid:uuid>/", views.DriverDetailView.as_view(), name="driver_detail"),
    path("drivers/<uuid:uuid>/edit/", views.DriverUpdateView.as_view(), name="driver_edit"),
    path("drivers/<uuid:uuid>/assign-vehicle/", views.driver_assign_vehicle, name="driver_assign_vehicle"),
    path(
        "drivers/<uuid:uuid>/unassign-vehicle/<int:assignment_id>/",
        views.driver_unassign_vehicle,
        name="driver_unassign_vehicle",
    ),
    path("drivers/<uuid:uuid>/deactivate/", views.driver_deactivate, name="driver_deactivate"),
    path("drivers/<uuid:uuid>/activate/", views.driver_activate, name="driver_activate"),
]
