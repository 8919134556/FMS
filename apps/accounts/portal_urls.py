from django.urls import path

from apps.accounts import portal_views as views

app_name = "accounts_portal"

urlpatterns = [
    path("quick-search/", views.quick_search, name="quick_search"),
    # Users
    path("users/", views.UserListView.as_view(), name="user_list"),
    path("users/export/", views.UserExportView.as_view(), name="user_export"),
    path("users/create/", views.UserCreateView.as_view(), name="user_create"),
    path("users/<uuid:uuid>/", views.UserDetailView.as_view(), name="user_detail"),
    path("users/<uuid:uuid>/edit/", views.UserUpdateView.as_view(), name="user_edit"),
    path("users/<uuid:uuid>/activate/", views.user_activate, name="user_activate"),
    path("users/<uuid:uuid>/deactivate/", views.user_deactivate, name="user_deactivate"),
    path("users/<uuid:uuid>/lock/", views.user_lock, name="user_lock"),
    path("users/<uuid:uuid>/unlock/", views.user_unlock, name="user_unlock"),
    path("users/<uuid:uuid>/force-password-reset/", views.user_force_password_reset, name="user_force_password_reset"),
    # Roles
    path("roles/", views.RoleListView.as_view(), name="role_list"),
    path("roles/create/", views.RoleCreateView.as_view(), name="role_create"),
    path("roles/<uuid:uuid>/", views.RoleDetailView.as_view(), name="role_detail"),
    path("roles/<uuid:uuid>/edit/", views.RoleUpdateView.as_view(), name="role_edit"),
    path("roles/<uuid:uuid>/permissions/", views.role_update_permissions, name="role_update_permissions"),
]
