from django.urls import path

from apps.contracts import views

app_name = "contracts"

urlpatterns = [
    path("", views.ContractListView.as_view(), name="contract_list"),
    path("export/", views.ContractExportView.as_view(), name="contract_export"),
    path("create/", views.ContractCreateView.as_view(), name="contract_create"),
    path("<uuid:uuid>/", views.ContractDetailView.as_view(), name="contract_detail"),
    path("<uuid:uuid>/edit/", views.ContractUpdateView.as_view(), name="contract_edit"),
    path("<uuid:uuid>/terminate/", views.contract_terminate, name="contract_terminate"),
    path("<uuid:uuid>/renew/", views.contract_renew, name="contract_renew"),
]
