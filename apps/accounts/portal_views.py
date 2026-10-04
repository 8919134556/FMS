"""CRUD and lifecycle actions for the User and Role entities.

Mounted at ``/admin/`` (see ``apps/accounts/portal_urls.py``). This is the
custom admin portal UI — distinct from Django's built-in ``/django-admin/``.
"""

import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordResetForm
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from apps.accounts.forms import RoleForm, UserCreateForm, UserUpdateForm
from apps.accounts.models import Permission, Role, RolePermission, User
from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.core.permissions import ModulePermissionRequiredMixin, module_permission_required, user_has_permission
from apps.core.utils import model_to_dict_safe

USER_AUDIT_FIELDS = [
    "employee_id", "username", "first_name", "last_name", "email", "mobile_number",
    "role_id", "status", "designation", "department",
]


@login_required
def quick_search(request):
    """Powers the navbar command palette (Ctrl/Cmd+K). Real DB lookups only —
    currently searches Users, the only entity built so far; each future phase
    that adds a searchable entity extends this view rather than the frontend
    faking results for modules that don't exist yet."""
    query = request.GET.get("q", "").strip()
    results = []
    if query and user_has_permission(request.user, "user", "view"):
        results = list(
            User.objects.filter(
                Q(first_name__icontains=query)
                | Q(last_name__icontains=query)
                | Q(username__icontains=query)
                | Q(employee_id__icontains=query)
                | Q(email__icontains=query)
            )[:8]
        )
    return render(request, "accounts/_quick_search_results.html", {"query": query, "results": results})


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
class UserListView(ModulePermissionRequiredMixin, ListView):
    model = User
    template_name = "accounts/user_list.html"
    context_object_name = "users"
    paginate_by = 25
    permission_module = "user"
    permission_action = "view"

    SORTABLE_FIELDS = {"username", "employee_id", "first_name", "email", "status", "created_at"}

    def get_queryset(self):
        qs = User.objects.select_related("role", "client").all()

        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(
                Q(username__icontains=query)
                | Q(employee_id__icontains=query)
                | Q(first_name__icontains=query)
                | Q(last_name__icontains=query)
                | Q(email__icontains=query)
                | Q(mobile_number__icontains=query)
            )

        status = self.request.GET.get("status", "").strip()
        if status:
            qs = qs.filter(status=status)

        role_id = self.request.GET.get("role", "").strip()
        if role_id:
            qs = qs.filter(role_id=role_id)

        sort = self.request.GET.get("sort", "").lstrip("-")
        if sort in self.SORTABLE_FIELDS:
            ordering = self.request.GET.get("sort")
            qs = qs.order_by(ordering)
        else:
            qs = qs.order_by("first_name", "last_name")

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        q = self.request.GET.get("q", "")
        status = self.request.GET.get("status", "")
        role = self.request.GET.get("role", "")
        context["current_filters"] = {"q": q, "status": status, "role": role, "sort": self.request.GET.get("sort", "")}
        context["filter_fields"] = [
            {"type": "search", "name": "q", "label": "Search", "value": q, "col": 4,
             "placeholder": "Name, email, employee ID, mobile…"},
            {"type": "select", "name": "status", "label": "Status", "value": status, "col": 3,
             "choices": User.Status.choices},
            {"type": "select", "name": "role", "label": "Role", "value": role, "col": 3,
             "choices": [(str(r.id), r.name) for r in Role.objects.filter(is_active=True)]},
        ]
        querystring = self.request.GET.urlencode()
        context["export_url"] = reverse("accounts_portal:user_export") + (f"?{querystring}" if querystring else "")
        context["create_url"] = reverse("accounts_portal:user_create")
        return context


class UserExportView(UserListView):
    """Streams the currently filtered user list as CSV instead of rendering HTML."""

    permission_module = "user"
    permission_action = "export"
    paginate_by = None

    def get(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="users_export.csv"'
        writer = csv.writer(response)
        writer.writerow(["Employee ID", "Username", "Full Name", "Email", "Mobile", "Role", "Status", "Date Joined"])
        for user in queryset:
            writer.writerow(
                [
                    user.employee_id,
                    user.username,
                    user.get_full_name(),
                    user.email,
                    user.mobile_number,
                    user.role.name if user.role else "",
                    user.get_status_display(),
                    user.date_of_joining or "",
                ]
            )
        log_action(
            action=AuditLog.Action.EXPORT,
            module="user",
            entity="User",
            entity_id="",
            new_value={"row_count": queryset.count()},
            user=request.user,
            request=request,
        )
        return response


class UserDetailView(ModulePermissionRequiredMixin, DetailView):
    model = User
    template_name = "accounts/user_detail.html"
    context_object_name = "user_obj"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "user"
    permission_action = "view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["direct_reports"] = self.object.direct_reports.all()
        context["recent_activity"] = AuditLog.objects.filter(
            module="user", entity="User", entity_id=str(self.object.pk)
        ).select_related("user")[:20]
        return context


class UserCreateView(ModulePermissionRequiredMixin, CreateView):
    model = User
    form_class = UserCreateForm
    template_name = "accounts/user_form.html"
    permission_module = "user"
    permission_action = "create"

    def form_valid(self, form):
        self.object = form.save(created_by=self.request.user)
        log_action(
            action=AuditLog.Action.CREATE,
            module="user",
            entity="User",
            entity_id=str(self.object.pk),
            new_value=model_to_dict_safe(self.object, USER_AUDIT_FIELDS),
            user=self.request.user,
            request=self.request,
        )
        messages.success(self.request, f"User '{self.object.username}' was created successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("accounts_portal:user_detail", kwargs={"uuid": self.object.uuid})


class UserUpdateView(ModulePermissionRequiredMixin, UpdateView):
    model = User
    form_class = UserUpdateForm
    template_name = "accounts/user_form.html"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "user"
    permission_action = "update"

    def form_valid(self, form):
        old_value = model_to_dict_safe(self.get_object(), USER_AUDIT_FIELDS)
        self.object = form.save(updated_by=self.request.user)
        log_action(
            action=AuditLog.Action.UPDATE,
            module="user",
            entity="User",
            entity_id=str(self.object.pk),
            old_value=old_value,
            new_value=model_to_dict_safe(self.object, USER_AUDIT_FIELDS),
            user=self.request.user,
            request=self.request,
        )
        messages.success(self.request, f"User '{self.object.username}' was updated successfully.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("accounts_portal:user_detail", kwargs={"uuid": self.object.uuid})


def _set_user_status(request, uuid, new_status, audit_note):
    user = get_object_or_404(User, uuid=uuid)
    old_status = user.status
    user.status = new_status
    user.updated_by = request.user
    user.save(update_fields=["status", "is_active", "updated_by", "updated_at"])
    log_action(
        action=AuditLog.Action.UPDATE,
        module="user",
        entity="User",
        entity_id=str(user.pk),
        old_value={"status": old_status},
        new_value={"status": new_status},
        user=request.user,
        request=request,
    )
    messages.success(request, f"{user.get_full_name()} {audit_note}.")
    return redirect("accounts_portal:user_detail", uuid=user.uuid)


@require_POST
@module_permission_required("user", "archive")
def user_activate(request, uuid):
    return _set_user_status(request, uuid, User.Status.ACTIVE, "has been activated")


@require_POST
@module_permission_required("user", "archive")
def user_deactivate(request, uuid):
    return _set_user_status(request, uuid, User.Status.INACTIVE, "has been deactivated")


@require_POST
@module_permission_required("user", "archive")
def user_lock(request, uuid):
    return _set_user_status(request, uuid, User.Status.LOCKED, "has been locked out")


@require_POST
@module_permission_required("user", "archive")
def user_unlock(request, uuid):
    return _set_user_status(request, uuid, User.Status.ACTIVE, "has been unlocked")


@require_POST
@module_permission_required("user", "update")
def user_force_password_reset(request, uuid):
    user = get_object_or_404(User, uuid=uuid)
    form = PasswordResetForm({"email": user.email})
    if form.is_valid():
        form.save(
            request=request,
            email_template_name="registration/password_reset_email.txt",
            subject_template_name="registration/password_reset_subject.txt",
        )
        log_action(
            action=AuditLog.Action.UPDATE,
            module="user",
            entity="User",
            entity_id=str(user.pk),
            new_value={"action": "force_password_reset_email_sent"},
            user=request.user,
            request=request,
        )
        messages.success(request, f"A password reset email has been sent to {user.email}.")
    else:
        messages.error(request, "Could not send a password reset email for this account.")
    return redirect("accounts_portal:user_detail", uuid=user.uuid)


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
class RoleListView(ModulePermissionRequiredMixin, ListView):
    model = Role
    template_name = "accounts/role_list.html"
    context_object_name = "roles"
    paginate_by = 25
    permission_module = "role"
    permission_action = "view"

    def get_queryset(self):
        qs = Role.objects.all().order_by("name")
        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(Q(name__icontains=query) | Q(code__icontains=query))
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        q = self.request.GET.get("q", "")
        context["current_filters"] = {"q": q}
        context["filter_fields"] = [
            {"type": "search", "name": "q", "label": "Search", "value": q, "col": 6, "placeholder": "Role name or code…"},
        ]
        context["create_url"] = reverse("accounts_portal:role_create")
        return context


class RoleDetailView(ModulePermissionRequiredMixin, DetailView):
    model = Role
    template_name = "accounts/role_detail.html"
    context_object_name = "role"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "role"
    permission_action = "view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        assigned_ids = set(self.object.permissions.values_list("id", flat=True))
        modules = {}
        for permission in Permission.objects.all():
            modules.setdefault(permission.get_module_display(), []).append(
                {"permission": permission, "assigned": permission.id in assigned_ids}
            )
        context["permission_matrix"] = dict(sorted(modules.items()))
        context["member_count"] = self.object.users.count()
        return context


class RoleCreateView(ModulePermissionRequiredMixin, CreateView):
    model = Role
    form_class = RoleForm
    template_name = "accounts/role_form.html"
    permission_module = "role"
    permission_action = "create"

    def form_valid(self, form):
        response = super().form_valid(form)
        log_action(
            action=AuditLog.Action.CREATE,
            module="role",
            entity="Role",
            entity_id=str(self.object.pk),
            new_value={"name": self.object.name, "code": self.object.code},
            user=self.request.user,
            request=self.request,
        )
        messages.success(self.request, f"Role '{self.object.name}' was created.")
        return response

    def get_success_url(self):
        return reverse("accounts_portal:role_detail", kwargs={"uuid": self.object.uuid})


class RoleUpdateView(ModulePermissionRequiredMixin, UpdateView):
    model = Role
    form_class = RoleForm
    template_name = "accounts/role_form.html"
    slug_field = "uuid"
    slug_url_kwarg = "uuid"
    permission_module = "role"
    permission_action = "update"

    def form_valid(self, form):
        response = super().form_valid(form)
        log_action(
            action=AuditLog.Action.UPDATE,
            module="role",
            entity="Role",
            entity_id=str(self.object.pk),
            new_value={"name": self.object.name, "is_active": self.object.is_active},
            user=self.request.user,
            request=self.request,
        )
        messages.success(self.request, f"Role '{self.object.name}' was updated.")
        return response

    def get_success_url(self):
        return reverse("accounts_portal:role_detail", kwargs={"uuid": self.object.uuid})


@require_POST
@module_permission_required("role", "update")
def role_update_permissions(request, uuid):
    role = get_object_or_404(Role, uuid=uuid)
    submitted_ids = {int(pk) for pk in request.POST.getlist("permissions")}
    current_ids = set(role.permissions.values_list("id", flat=True))

    to_add = submitted_ids - current_ids
    to_remove = current_ids - submitted_ids

    RolePermission.objects.filter(role=role, permission_id__in=to_remove).delete()
    RolePermission.objects.bulk_create(
        [RolePermission(role=role, permission_id=pk, assigned_by=request.user) for pk in to_add]
    )

    log_action(
        action=AuditLog.Action.UPDATE,
        module="role",
        entity="Role",
        entity_id=str(role.pk),
        old_value={"permission_ids": sorted(current_ids)},
        new_value={"permission_ids": sorted(submitted_ids)},
        user=request.user,
        request=request,
    )
    messages.success(request, f"Permissions for '{role.name}' were updated.")
    return redirect("accounts_portal:role_detail", uuid=role.uuid)
