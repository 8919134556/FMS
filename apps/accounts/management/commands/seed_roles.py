from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import Permission, Role, RolePermission

# Which actions are meaningful for each module. Kept as an explicit map
# (rather than a blind Module x Action cross product) so the seeded catalog
# only contains permissions that a role could plausibly need — e.g. an audit
# log can be viewed/exported but never created or archived.
MODULE_ACTIONS = {
    "user": ["view", "create", "update", "delete", "archive", "export"],
    "role": ["view", "create", "update", "delete"],
    "client": ["view", "create", "update", "archive", "export"],
    "site": ["view", "create", "update", "archive"],
    "vendor": ["view", "create", "update", "archive"],
    "branch": ["view", "create", "update", "archive"],
    "contract": ["view", "create", "update", "archive", "approve"],
    "vehicle": ["view", "create", "update", "archive", "export"],
    "vehicle_type": ["view", "create", "update", "archive"],
    "driver": ["view", "create", "update", "archive", "export"],
    "assignment": ["view", "create", "update", "archive"],
    "document": ["view", "create", "update", "archive", "delete", "export"],
    "insurance": ["view", "create", "update", "archive"],
    "tracking_device": ["view", "create", "update", "archive", "command"],
    "maintenance": ["view", "create", "update", "archive", "export"],
    "route": ["view", "create", "update", "archive"],
    "trip": ["view", "create", "update", "archive", "export"],
    "geofence": ["view", "create", "update", "archive"],
    "alert": ["view", "update", "archive"],
    "notification": ["view", "create", "update", "archive"],
    "master_data": ["view", "create", "update", "archive"],
    "audit_log": ["view", "export"],
    "settings": ["view", "update"],
}


def _manage_actions(module):
    """view + create + update + archive (+ export if the module has it) — everything short of hard delete."""
    actions = [a for a in MODULE_ACTIONS.get(module, []) if a != "delete"]
    return [(module, a) for a in actions]


def _view_only(module):
    return [(module, "view")] if "view" in MODULE_ACTIONS.get(module, []) else []


ROLE_DEFINITIONS = {
    "super-admin": {
        "name": "Super Admin",
        "description": "Unrestricted access to every module, including role/permission management.",
        "permissions": "ALL",
    },
    "admin": {
        "name": "Admin",
        "description": "Full operational access; cannot delete roles.",
        "permissions": [
            perm
            for module in MODULE_ACTIONS
            for perm in ([(module, a) for a in MODULE_ACTIONS[module] if a != "delete"])
        ],
    },
    "fleet-manager": {
        "name": "Fleet Manager",
        "description": "Owns the vehicle and driver fleet: vehicles, drivers, assignments, maintenance, tracking.",
        "permissions": (
            _manage_actions("vehicle")
            + _manage_actions("vehicle_type")
            + _manage_actions("driver")
            + _manage_actions("assignment")
            + _manage_actions("maintenance")
            + _manage_actions("tracking_device")
            + _view_only("client")
            + _view_only("document")
            + _view_only("insurance")
        ),
    },
    "operations-manager": {
        "name": "Operations Manager",
        "description": "Owns day-to-day operations: trips, routes, geofences, alerts.",
        "permissions": (
            _manage_actions("trip")
            + _manage_actions("route")
            + _manage_actions("geofence")
            + _manage_actions("alert")
            + _view_only("vehicle")
            + _view_only("driver")
            + _view_only("client")
        ),
    },
    "dispatcher": {
        "name": "Dispatcher",
        "description": "Creates and updates trips; read-only on the rest of the fleet.",
        "permissions": (
            [("trip", "view"), ("trip", "create"), ("trip", "update")]
            + _view_only("vehicle")
            + _view_only("driver")
            + _view_only("route")
            + [("alert", "view"), ("alert", "update")]
        ),
    },
    "client-admin": {
        "name": "Client Admin",
        "description": "Read-only visibility into their own client's fleet, trips, documents, and vehicle alerts.",
        "permissions": (
            _view_only("client")
            + _view_only("site")
            + _view_only("vehicle")
            + _view_only("driver")
            + _view_only("trip")
            + _view_only("contract")
            + _view_only("document")
            + _view_only("alert")
        ),
    },
    "read-only": {
        "name": "Read Only User",
        "description": "View access across every module. No create/update/archive rights anywhere.",
        "permissions": [(module, "view") for module in MODULE_ACTIONS if "view" in MODULE_ACTIONS[module]],
    },
}


class Command(BaseCommand):
    help = "Seed the Permission catalog and the standard FMS roles (idempotent — safe to re-run)."

    @transaction.atomic
    def handle(self, *args, **options):
        permission_count = self._seed_permissions()
        role_count = self._seed_roles()
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded/verified {permission_count} permissions and {role_count} roles."
            )
        )

    def _seed_permissions(self):
        created = 0
        for module, actions in MODULE_ACTIONS.items():
            for action in actions:
                _, was_created = Permission.objects.get_or_create(module=module, action=action)
                created += int(was_created)
        self.stdout.write(f"  Permissions: {created} created, rest already existed.")
        return Permission.objects.count()

    def _seed_roles(self):
        all_permission_ids = set(Permission.objects.values_list("id", flat=True))
        for code, definition in ROLE_DEFINITIONS.items():
            role, _ = Role.objects.update_or_create(
                code=code,
                defaults={
                    "name": definition["name"],
                    "description": definition["description"],
                    "is_system_role": True,
                    "is_active": True,
                },
            )
            if definition["permissions"] == "ALL":
                permission_ids = all_permission_ids
            else:
                wanted_codes = {f"{m}.{a}" for m, a in definition["permissions"]}
                permission_ids = set(
                    Permission.objects.filter(code__in=wanted_codes).values_list("id", flat=True)
                )

            existing_ids = set(role.permissions.values_list("id", flat=True))
            RolePermission.objects.filter(role=role, permission_id__in=existing_ids - permission_ids).delete()
            RolePermission.objects.bulk_create(
                [RolePermission(role=role, permission_id=pk) for pk in permission_ids - existing_ids]
            )
            self.stdout.write(f"  Role '{role.name}': {len(permission_ids)} permissions.")
        return Role.objects.count()
