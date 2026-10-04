from rest_framework.permissions import BasePermission

from apps.core.permissions import user_has_permission

_ACTION_MAP = {
    "list": "view",
    "retrieve": "view",
    "create": "create",
    "update": "update",
    "partial_update": "update",
    "destroy": "archive",
    # Custom @action names on ViewSets. Activate/deactivate are "archive"
    # in the RBAC catalog (same as the portal's user_activate/deactivate
    # views) — an unmapped action name used to fail closed for everyone
    # except superusers because no such permission exists.
    "activate": "archive",
    "deactivate": "archive",
}


class HasModulePermission(BasePermission):
    """DRF permission class backed by the same RBAC used by the template views.

    Viewsets declare ``permission_module = "vehicle"`` (etc.); this maps the
    DRF action name (list/create/update/destroy/...) to our view/create/update/
    archive vocabulary and checks it against the caller's role.
    """

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        module = getattr(view, "permission_module", None)
        action = _ACTION_MAP.get(view.action, view.action)
        return user_has_permission(request.user, module, action)
