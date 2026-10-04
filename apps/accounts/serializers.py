from rest_framework import serializers

from apps.accounts.models import Permission, Role, User


class PermissionSerializer(serializers.ModelSerializer):
    module_display = serializers.CharField(source="get_module_display", read_only=True)
    action_display = serializers.CharField(source="get_action_display", read_only=True)

    class Meta:
        model = Permission
        fields = ["uuid", "module", "module_display", "action", "action_display", "code", "description"]
        read_only_fields = ["uuid", "code"]


class RoleSerializer(serializers.ModelSerializer):
    permission_count = serializers.IntegerField(source="permissions.count", read_only=True)
    user_count = serializers.IntegerField(source="users.count", read_only=True)

    class Meta:
        model = Role
        fields = [
            "uuid", "name", "code", "description", "is_system_role", "is_active",
            "permission_count", "user_count", "created_at", "updated_at",
        ]
        read_only_fields = ["uuid", "is_system_role", "created_at", "updated_at"]


class UserSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="get_full_name", read_only=True)
    role_name = serializers.CharField(source="role.name", read_only=True, default=None)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = User
        fields = [
            "uuid", "employee_id", "username", "full_name", "first_name", "middle_name", "last_name",
            "email", "mobile_number", "alternate_mobile_number", "designation", "department",
            "role", "role_name", "reporting_manager", "city", "state", "country", "pincode",
            "date_of_joining", "status", "status_display", "is_active", "last_login",
            "created_at", "updated_at",
        ]
        read_only_fields = ["uuid", "is_active", "last_login", "created_at", "updated_at"]

    def validate_mobile_number(self, value):
        qs = User.objects.filter(mobile_number=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A user with this mobile number already exists.")
        return value


class UserCreateSerializer(UserSerializer):
    password = serializers.CharField(write_only=True, min_length=10)

    class Meta(UserSerializer.Meta):
        fields = UserSerializer.Meta.fields + ["password"]

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User(**validated_data)
        user.set_password(password)
        request = self.context.get("request")
        if request is not None:
            user.created_by = request.user
        user.save()
        return user
