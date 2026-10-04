from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.models import Permission, Role, User
from apps.accounts.serializers import (
    PermissionSerializer,
    RoleSerializer,
    UserCreateSerializer,
    UserSerializer,
)
from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.core.api_permissions import HasModulePermission
from apps.core.pagination import StandardResultsSetPagination


class UserViewSet(viewsets.ModelViewSet):
    """CRUD + activate/deactivate for FMS staff users. See ``/api/docs/`` for schema."""

    queryset = User.objects.select_related("role").all().order_by("first_name", "last_name")
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "user"
    pagination_class = StandardResultsSetPagination
    filterset_fields = ["status", "role"]
    search_fields = ["username", "employee_id", "email", "first_name", "last_name", "mobile_number"]
    ordering_fields = ["username", "employee_id", "first_name", "status", "created_at"]
    lookup_field = "uuid"

    def get_serializer_class(self):
        if self.action == "create":
            return UserCreateSerializer
        return UserSerializer

    def perform_create(self, serializer):
        instance = serializer.save()
        log_action(
            action=AuditLog.Action.CREATE,
            module="user",
            entity="User",
            entity_id=str(instance.pk),
            new_value={"username": instance.username, "status": instance.status},
            user=self.request.user,
            request=self.request,
        )

    def perform_update(self, serializer):
        instance = serializer.save()
        log_action(
            action=AuditLog.Action.UPDATE,
            module="user",
            entity="User",
            entity_id=str(instance.pk),
            new_value={"username": instance.username, "status": instance.status},
            user=self.request.user,
            request=self.request,
        )

    def perform_destroy(self, instance):
        instance.delete(deleted_by=self.request.user)
        log_action(
            action=AuditLog.Action.ARCHIVE,
            module="user",
            entity="User",
            entity_id=str(instance.pk),
            user=self.request.user,
            request=self.request,
        )

    @action(detail=True, methods=["post"], url_path="activate")
    def activate(self, request, uuid=None):
        user = self.get_object()
        user.status = User.Status.ACTIVE
        user.save(update_fields=["status", "is_active", "updated_at"])
        log_action(
            action=AuditLog.Action.UPDATE, module="user", entity="User", entity_id=str(user.pk),
            new_value={"status": user.status}, user=request.user, request=request,
        )
        return Response(UserSerializer(user).data)

    @action(detail=True, methods=["post"], url_path="deactivate")
    def deactivate(self, request, uuid=None):
        user = self.get_object()
        user.status = User.Status.INACTIVE
        user.save(update_fields=["status", "is_active", "updated_at"])
        log_action(
            action=AuditLog.Action.UPDATE, module="user", entity="User", entity_id=str(user.pk),
            new_value={"status": user.status}, user=request.user, request=request,
        )
        return Response(UserSerializer(user).data)


class RoleViewSet(viewsets.ModelViewSet):
    queryset = Role.objects.all().order_by("name")
    serializer_class = RoleSerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "role"
    pagination_class = StandardResultsSetPagination
    filterset_fields = ["is_active", "is_system_role"]
    search_fields = ["name", "code"]
    ordering_fields = ["name", "created_at"]
    lookup_field = "uuid"

    def perform_create(self, serializer):
        instance = serializer.save()
        log_action(
            action=AuditLog.Action.CREATE, module="role", entity="Role", entity_id=str(instance.pk),
            new_value={"name": instance.name}, user=self.request.user, request=self.request,
        )

    def perform_update(self, serializer):
        instance = serializer.save()
        log_action(
            action=AuditLog.Action.UPDATE, module="role", entity="Role", entity_id=str(instance.pk),
            new_value={"name": instance.name}, user=self.request.user, request=self.request,
        )


class PermissionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Permission.objects.all().order_by("module", "action")
    serializer_class = PermissionSerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "role"
    pagination_class = StandardResultsSetPagination
    filterset_fields = ["module", "action"]
    search_fields = ["code", "description"]
    lookup_field = "uuid"
