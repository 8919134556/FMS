from django import forms

from apps.core.forms import BootstrapFormMixin
from apps.core.validators import validate_document_content, validate_document_extension, validate_file_size
from apps.documents.models import Document
from apps.documents.registry import ENTITY_TYPE_CHOICES, get_entity_config


class DocumentForm(BootstrapFormMixin, forms.Form):
    document_type = forms.ChoiceField(choices=Document.DocumentType.choices)
    title = forms.CharField(max_length=200)
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    file = forms.FileField(validators=[validate_file_size, validate_document_extension, validate_document_content])
    issue_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    expiry_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    entity_type = forms.ChoiceField(choices=ENTITY_TYPE_CHOICES, label="Attach To")
    object_id = forms.IntegerField(widget=forms.HiddenInput, label="Related Record")

    def clean(self):
        cleaned = super().clean()
        entity_type = cleaned.get("entity_type")
        object_id = cleaned.get("object_id")
        if entity_type and object_id is not None:
            config = get_entity_config(entity_type)
            if not config or not config["model"].objects.filter(pk=object_id).exists():
                self.add_error("object_id", "Please select a valid record to attach this document to.")
        return cleaned

    def get_content_object(self):
        config = get_entity_config(self.cleaned_data["entity_type"])
        return config["model"].objects.get(pk=self.cleaned_data["object_id"])


class DocumentEditForm(BootstrapFormMixin, forms.Form):
    """Metadata-only edit — the file itself is immutable once uploaded;
    use the Replace workflow (apps.documents.services.replace_document) to
    swap the file while keeping version history."""

    document_type = forms.ChoiceField(choices=Document.DocumentType.choices)
    title = forms.CharField(max_length=200)
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    issue_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    expiry_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


class DocumentReplaceForm(BootstrapFormMixin, forms.Form):
    file = forms.FileField(
        label="New File",
        validators=[validate_file_size, validate_document_extension, validate_document_content],
    )
