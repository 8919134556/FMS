from django.urls import path

from apps.vehicles import views

app_name = "vehicles"

urlpatterns = [
    path("vehicles/", views.VehicleListView.as_view(), name="vehicle_list"),
    path("vehicles/export/", views.VehicleExportView.as_view(), name="vehicle_export"),
    path("vehicles/create/", views.VehicleCreateView.as_view(), name="vehicle_create"),
    path("vehicles/<uuid:uuid>/", views.VehicleDetailView.as_view(), name="vehicle_detail"),
    path("vehicles/<uuid:uuid>/edit/", views.VehicleUpdateView.as_view(), name="vehicle_edit"),
    path("vehicles/<uuid:uuid>/assign-driver/", views.vehicle_assign_driver, name="vehicle_assign_driver"),
    path(
        "vehicles/<uuid:uuid>/unassign-driver/<int:assignment_id>/",
        views.vehicle_unassign_driver,
        name="vehicle_unassign_driver",
    ),
    path("vehicles/<uuid:uuid>/deactivate/", views.vehicle_deactivate, name="vehicle_deactivate"),
    path("vehicles/<uuid:uuid>/activate/", views.vehicle_activate, name="vehicle_activate"),
    path("vehicle-types/", views.VehicleTypeListView.as_view(), name="vehicle_type_list"),
    path("vehicle-types/create/", views.VehicleTypeCreateView.as_view(), name="vehicle_type_create"),
    path("vehicle-types/<uuid:uuid>/edit/", views.VehicleTypeUpdateView.as_view(), name="vehicle_type_edit"),
    path("vehicle-types/<uuid:uuid>/deactivate/", views.vehicle_type_deactivate, name="vehicle_type_deactivate"),
    path("vehicle-types/<uuid:uuid>/activate/", views.vehicle_type_activate, name="vehicle_type_activate"),
    path("assignments/", views.AssignmentListView.as_view(), name="assignment_list"),
]
