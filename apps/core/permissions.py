from functools import wraps

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied

from apps.core.scoping import CLIENT_SCOPED_MODULES, CLIENT_USER_ACTIONS


def user_has_permission(user, module: str, action: str) -> bool:
    """Single source of truth for RBAC checks, used by views, templates, and the API.

    Superusers always pass. Everyone else needs an active role whose permission
    set includes the exact (module, action) pair.

    The full (module, action) set for the role is fetched once and cached on
    the ``user`` instance — ``request.user`` is the same Python object across
    a request's context processors, view, and template rendering, and the
    sidebar nav alone checks ~30 distinct pairs, so one query up front beats
    one query per pair. Scoped to the object, not the DB row, so it can
    never leak across requests.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return True
    # Client users (User.client set) are a hard-limited tier on top of RBAC:
    # read-only, and only for modules that apply client scoping. A role can
    # never widen this — see apps.core.scoping.
    if getattr(user, "client_id", None) is not None and (
        module not in CLIENT_SCOPED_MODULES or action not in CLIENT_USER_ACTIONS
    ):
        return False
    role = getattr(user, "role", None)
    if not role or not role.is_active:
        return False
    if "_fms_permission_set" not in user.__dict__:
        user.__dict__["_fms_permission_set"] = set(role.permissions.values_list("module", "action"))
    return (module, action) in user.__dict__["_fms_permission_set"]


# Modules that make up the administration area. Holding "view" on any of
# them is what earns the admin dashboard (users/roles/audit/settings
# widgets) — decided by permission, not by a hardcoded role name, so a
# custom role with e.g. only audit_log.view is treated consistently.
ADMIN_AREA_MODULES = ("user", "role", "audit_log", "settings")


def user_has_any_permission(user, modules, action="view") -> bool:
    return any(user_has_permission(user, module, action) for module in modules)


def has_admin_access(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    return bool(user.is_superuser) or user_has_any_permission(user, ADMIN_AREA_MODULES)


def restrict_detail_tabs(user, context, tab_modules):
    """Detail pages (vehicle, client, trip, ...) bundle tabs that read *other*
    modules' data — a vehicle's trips, a client's contracts, every record's
    audit history. Holding ``vehicle.view`` must not double as access to all
    of that, so drop each tab whose backing module the viewer can't open.

    If the requested tab was one of them, fall back to Overview instead of
    rendering it. ``tab_modules`` maps tab id -> the RBAC module that owns
    the tab's data; tabs not listed are the record's own data and stay.
    """
    allowed = [
        tab for tab in context["tab_list"]
        if tab["id"] not in tab_modules or user_has_permission(user, tab_modules[tab["id"]], "view")
    ]
    context["tab_list"] = allowed
    if context.get("active_tab") not in {tab["id"] for tab in allowed}:
        context["active_tab"] = "overview"
    return context


class ModulePermissionRequiredMixin(LoginRequiredMixin):
    """Class-based view mixin: set ``permission_module`` and ``permission_action``."""

    permission_module: str | None = None
    permission_action: str | None = None

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)
        if not user_has_permission(request.user, self.permission_module, self.permission_action):
            raise PermissionDenied("You do not have permission to perform this action.")
        return super().dispatch(request, *args, **kwargs)


def module_permission_required(module: str, action: str):
    """Function-based view decorator equivalent of ``ModulePermissionRequiredMixin``."""

    def decorator(view_func):
        @wraps(view_func)
        @login_required
        def _wrapped(request, *args, **kwargs):
            if not user_has_permission(request.user, module, action):
                raise PermissionDenied("You do not have permission to perform this action.")
            return view_func(request, *args, **kwargs)

        return _wrapped

    return decorator
