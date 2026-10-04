from django.urls import path

from apps.notifications import views

app_name = "notifications"

urlpatterns = [
    path("", views.NotificationListView.as_view(), name="notification_list"),
    path("feed/", views.NotificationFeedView.as_view(), name="notification_feed"),
    path("<uuid:uuid>/read/", views.mark_read, name="notification_mark_read"),
    path("mark-all-read/", views.mark_all_read, name="notification_mark_all_read"),
    path("<uuid:uuid>/archive/", views.archive, name="notification_archive"),
    path("broadcast/", views.BroadcastNotificationView.as_view(), name="broadcast"),
]
