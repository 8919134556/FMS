from django.urls import path

from apps.vendors import views

app_name = "vendors"

urlpatterns = [
    path("", views.VendorListView.as_view(), name="vendor_list"),
    path("export/", views.VendorExportView.as_view(), name="vendor_export"),
    path("create/", views.VendorCreateView.as_view(), name="vendor_create"),
    path("<uuid:uuid>/", views.VendorDetailView.as_view(), name="vendor_detail"),
    path("<uuid:uuid>/edit/", views.VendorUpdateView.as_view(), name="vendor_edit"),
    path("<uuid:uuid>/deactivate/", views.vendor_deactivate, name="vendor_deactivate"),
    path("<uuid:uuid>/activate/", views.vendor_activate, name="vendor_activate"),
]
