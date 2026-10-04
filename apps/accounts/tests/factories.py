import factory
from factory.django import DjangoModelFactory

from apps.accounts.models import Permission, Role, User


class PermissionFactory(DjangoModelFactory):
    class Meta:
        model = Permission
        django_get_or_create = ("module", "action")

    module = Permission.Module.VEHICLE
    action = Permission.Action.VIEW


class RoleFactory(DjangoModelFactory):
    class Meta:
        model = Role
        django_get_or_create = ("code",)

    name = factory.Sequence(lambda n: f"Role {n}")
    code = factory.Sequence(lambda n: f"role-{n}")
    description = "Factory-generated test role."
    is_active = True


class UserFactory(DjangoModelFactory):
    class Meta:
        model = User
        skip_postgeneration_save = True

    employee_id = factory.Sequence(lambda n: f"EMP{n:05d}")
    username = factory.Sequence(lambda n: f"user{n}")
    first_name = "Test"
    last_name = "User"
    email = factory.Sequence(lambda n: f"user{n}@example.com")
    mobile_number = factory.Sequence(lambda n: f"+155500{n:05d}")
    status = User.Status.ACTIVE

    @factory.post_generation
    def set_password(self, create, extracted, **kwargs):
        self.set_password(extracted or "TestPass!1234")
        if create:
            self.save()
