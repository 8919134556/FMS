from django.urls import path

from apps.tracking import views

app_name = "tracking"

urlpatterns = [
    path("live/", views.LiveTrackingView.as_view(), name="live_map"),
    path("trip-report/", views.TripReportView.as_view(), name="trip_report"),
    path("odometer-report/", views.OdometerReportView.as_view(), name="odometer_report"),
    path("location-report/", views.LocationReportView.as_view(), name="location_report"),
    path("devices/", views.DeviceListView.as_view(), name="device_list"),
    path("devices/create/", views.DeviceCreateView.as_view(), name="device_create"),
    path("devices/<uuid:uuid>/", views.DeviceDetailView.as_view(), name="device_detail"),
    path("devices/<uuid:uuid>/edit/", views.DeviceUpdateView.as_view(), name="device_edit"),
    path("devices/<uuid:uuid>/assign/", views.device_assign, name="device_assign"),
    path("devices/<uuid:uuid>/unassign/", views.device_unassign, name="device_unassign"),
    path("devices/<uuid:uuid>/disable/", views.device_disable, name="device_disable"),
    path("devices/<uuid:uuid>/enable/", views.device_enable, name="device_enable"),
    path("devices/<uuid:uuid>/regenerate-key/", views.device_regenerate_key, name="device_regenerate_key"),
    path("devices/<uuid:uuid>/key/", views.device_key_reveal, name="device_key_reveal"),
    path("commands/", views.CommandListView.as_view(), name="command_list"),
    path("commands/create/", views.CommandCreateView.as_view(), name="command_create"),
    path("commands/<uuid:uuid>/", views.CommandDetailView.as_view(), name="command_detail"),
    path("commands/<uuid:uuid>/cancel/", views.command_cancel, name="command_cancel"),
    path("commands/<uuid:uuid>/retry/", views.command_retry, name="command_retry"),
]
