from django.views.generic import ListView

from apps.audit.models import AuditLog
from apps.core.permissions import ModulePermissionRequiredMixin


class AuditLogListView(ModulePermissionRequiredMixin, ListView):
    model = AuditLog
    template_name = "audit/audit_log_list.html"
    context_object_name = "logs"
    paginate_by = 50
    permission_module = "audit_log"
    permission_action = "view"

    def get_queryset(self):
        qs = AuditLog.objects.select_related("user").all()

        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(entity__icontains=query) | qs.filter(entity_id__icontains=query)

        action = self.request.GET.get("action", "").strip()
        if action:
            qs = qs.filter(action=action)

        module = self.request.GET.get("module", "").strip()
        if module:
            qs = qs.filter(module=module)

        date_from = self.request.GET.get("date_from", "").strip()
        if date_from:
            qs = qs.filter(timestamp__date__gte=date_from)

        date_to = self.request.GET.get("date_to", "").strip()
        if date_to:
            qs = qs.filter(timestamp__date__lte=date_to)

        return qs.order_by("-timestamp")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        q = self.request.GET.get("q", "")
        action = self.request.GET.get("action", "")
        module = self.request.GET.get("module", "")
        date_from = self.request.GET.get("date_from", "")
        date_to = self.request.GET.get("date_to", "")
        context["current_filters"] = {"q": q, "action": action, "module": module, "date_from": date_from, "date_to": date_to}
        module_choices = [(m, m) for m in AuditLog.objects.order_by("module").values_list("module", flat=True).distinct()]
        context["filter_fields"] = [
            {"type": "search", "name": "q", "label": "Entity", "value": q, "col": 3, "placeholder": "Search entity or ID…"},
            {"type": "select", "name": "action", "label": "Action", "value": action, "col": 2, "choices": AuditLog.Action.choices},
            {"type": "select", "name": "module", "label": "Module", "value": module, "col": 2, "choices": module_choices},
            {"type": "date", "name": "date_from", "label": "From", "value": date_from, "col": 2},
            {"type": "date", "name": "date_to", "label": "To", "value": date_to, "col": 2},
        ]
        return context
