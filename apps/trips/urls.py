from django.urls import path

from apps.trips import views

app_name = "trips"

urlpatterns = [
    path("", views.TripListView.as_view(), name="trip_list"),
    path("export/", views.TripExportView.as_view(), name="trip_export"),
    path("create/", views.TripCreateView.as_view(), name="trip_create"),
    path("<uuid:uuid>/", views.TripDetailView.as_view(), name="trip_detail"),
    path("<uuid:uuid>/edit/", views.TripUpdateView.as_view(), name="trip_edit"),
    path("<uuid:uuid>/schedule/", views.trip_schedule, name="trip_schedule"),
    path("<uuid:uuid>/assign/", views.trip_assign, name="trip_assign"),
    path("<uuid:uuid>/dispatch/", views.trip_dispatch, name="trip_dispatch"),
    path("<uuid:uuid>/start/", views.trip_start, name="trip_start"),
    path("<uuid:uuid>/delay/", views.trip_delay, name="trip_delay"),
    path("<uuid:uuid>/resume/", views.trip_resume, name="trip_resume"),
    path("<uuid:uuid>/complete/", views.trip_complete, name="trip_complete"),
    path("<uuid:uuid>/cancel/", views.trip_cancel, name="trip_cancel"),
]
