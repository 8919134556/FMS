from django.urls import path

from apps.locations import views

app_name = "locations"

urlpatterns = [
    path("", views.SiteListView.as_view(), name="site_list"),
    path("export/", views.SiteExportView.as_view(), name="site_export"),
    path("create/", views.SiteCreateView.as_view(), name="site_create"),
    path("<uuid:uuid>/", views.SiteDetailView.as_view(), name="site_detail"),
    path("<uuid:uuid>/edit/", views.SiteUpdateView.as_view(), name="site_edit"),
    path("<uuid:uuid>/deactivate/", views.site_deactivate, name="site_deactivate"),
    path("<uuid:uuid>/activate/", views.site_activate, name="site_activate"),
    path("branches/", views.BranchListView.as_view(), name="branch_list"),
    path("branches/export/", views.BranchExportView.as_view(), name="branch_export"),
    path("branches/create/", views.BranchCreateView.as_view(), name="branch_create"),
    path("branches/<uuid:uuid>/", views.BranchDetailView.as_view(), name="branch_detail"),
    path("branches/<uuid:uuid>/edit/", views.BranchUpdateView.as_view(), name="branch_edit"),
    path("branches/<uuid:uuid>/deactivate/", views.branch_deactivate, name="branch_deactivate"),
    path("branches/<uuid:uuid>/activate/", views.branch_activate, name="branch_activate"),
]
