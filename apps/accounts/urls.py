from django.urls import path

from apps.accounts import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.FMSLoginView.as_view(), name="login"),
    path("logout/", views.FMSLogoutView.as_view(), name="logout"),
    path("password/change/", views.FMSPasswordChangeView.as_view(), name="password_change"),
    path("password/change/done/", views.FMSPasswordChangeDoneView.as_view(), name="password_change_done"),
    path("password/reset/", views.FMSPasswordResetView.as_view(), name="password_reset"),
    path("password/reset/done/", views.FMSPasswordResetDoneView.as_view(), name="password_reset_done"),
    path(
        "password/reset/confirm/<uidb64>/<token>/",
        views.FMSPasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path("password/reset/complete/", views.FMSPasswordResetCompleteView.as_view(), name="password_reset_complete"),
]
