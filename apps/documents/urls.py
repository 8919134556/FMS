from django.urls import path

from apps.documents import views

app_name = "documents"

urlpatterns = [
    path("", views.DocumentListView.as_view(), name="document_list"),
    path("export/", views.DocumentExportView.as_view(), name="document_export"),
    path("create/", views.DocumentCreateView.as_view(), name="document_create"),
    path("related-options/", views.document_related_options, name="document_related_options"),
    path("<uuid:uuid>/", views.DocumentDetailView.as_view(), name="document_detail"),
    path("<uuid:uuid>/edit/", views.DocumentUpdateView.as_view(), name="document_edit"),
    path("<uuid:uuid>/download/", views.document_download, name="document_download"),
    path("<uuid:uuid>/preview/", views.document_preview, name="document_preview"),
    path("<uuid:uuid>/replace/", views.document_replace, name="document_replace"),
    path("<uuid:uuid>/archive/", views.document_archive, name="document_archive"),
    path("<uuid:uuid>/restore/", views.document_restore, name="document_restore"),
    path("<uuid:uuid>/delete/", views.document_delete, name="document_delete"),
]
