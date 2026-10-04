from django.urls import path

from apps.trips import dispatch_views

app_name = "dispatch"

urlpatterns = [
    path("", dispatch_views.DispatchBoardView.as_view(), name="board"),
]
