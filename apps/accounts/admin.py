from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm

from apps.accounts.models import Permission, Role, RolePermission, User


class FMSUserCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email", "employee_id", "first_name", "last_name", "mobile_number")


class FMSUserChangeForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User
        fields = "__all__"


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    add_form = FMSUserCreationForm
    form = FMSUserChangeForm
    model = User
    ordering = ("first_name", "last_name")
    list_display = ("username", "employee_id", "email", "role", "status", "is_staff", "is_active")
    list_filter = ("status", "role", "is_staff", "is_active", "gender")
    search_fields = ("username", "employee_id", "email", "first_name", "last_name", "mobile_number")
    readonly_fields = ("uuid", "created_at", "updated_at", "last_login")
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        (
            "Personal info",
            {
                "fields": (
                    "employee_id",
                    "first_name",
                    "middle_name",
                    "last_name",
                    "email",
                    "mobile_number",
                    "alternate_mobile_number",
                    "profile_photo",
                    "date_of_birth",
                    "gender",
                )
            },
        ),
        ("Employment", {"fields": ("designation", "department", "role", "reporting_manager", "date_of_joining")}),
        ("Address", {"fields": ("address", "city", "state", "country", "pincode")}),
        ("Status", {"fields": ("status", "is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Important dates", {"fields": ("last_login", "created_at", "updated_at")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "username",
                    "email",
                    "employee_id",
                    "first_name",
                    "last_name",
                    "mobile_number",
                    "password1",
                    "password2",
                ),
            },
        ),
    )

    def get_queryset(self, request):
        return User.all_objects.all()


class RolePermissionInline(admin.TabularInline):
    model = RolePermission
    extra = 0
    autocomplete_fields = ["permission"]


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "is_system_role", "is_active", "created_at")
    list_filter = ("is_active", "is_system_role")
    search_fields = ("name", "code")
    prepopulated_fields = {"code": ("name",)}
    inlines = [RolePermissionInline]


@admin.register(Permission)
class PermissionAdmin(admin.ModelAdmin):
    list_display = ("code", "module", "action", "description")
    list_filter = ("module", "action")
    search_fields = ("code", "description")
    ordering = ("module", "action")
