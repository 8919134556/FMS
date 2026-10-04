"""Client (tenant) scoping — the ONE place that decides which client's data a
user may see.

Two independent layers protect data:

* RBAC (apps.core.permissions) answers *which modules/actions* a user has.
* Scoping (this module) answers *whose rows* — it applies to users linked to
  a client (``User.client``): client users.

A client user:
  - only ever sees rows belonging to ``user.client`` (``scope_queryset``);
  - is READ-ONLY: only ``view``/``export`` are ever granted;
  - is denied every module whose queries aren't client-aware
    (``CLIENT_SCOPED_MODULES`` is a whitelist — a new module is denied to
    client users until someone consciously adds scoping and lists it here).

Internal staff (``client`` empty) and superusers are unaffected.
"""

from django.db.models import Q

# Modules whose views/APIs/reports/dashboard widgets apply client scoping.
CLIENT_SCOPED_MODULES = frozenset(
    {
        "client", "site", "contract", "vehicle", "vehicle_type", "driver", "assignment",
        "trip", "maintenance", "document", "tracking_device", "route",
        # Alerts: vehicle alert events carry Alert.client; operational alerts have
        # none and so are never visible to a client user (apps.alerts.views/report).
        "alert",
    }
)
CLIENT_USER_ACTIONS = frozenset({"view", "export"})


def client_scope_id(user):
    """The client id this user is restricted to, or ``None`` for unrestricted
    (internal staff, superusers, anonymous)."""
    if not user or not getattr(user, "is_authenticated", False) or user.is_superuser:
        return None
    return getattr(user, "client_id", None)


def is_client_scoped(user):
    return client_scope_id(user) is not None


def scope_queryset(qs, user, lookup="client_id"):
    """Restrict ``qs`` to the user's client via ``lookup`` (a path to the
    client id, e.g. ``"client_id"`` or ``"vehicle__client_id"``). No-op for
    unrestricted users."""
    client_id = client_scope_id(user)
    if client_id is None:
        return qs
    return qs.filter(**{lookup: client_id})


def scope_queryset_including_shared(qs, user, lookup="client_id"):
    """Same, but rows with no client at all (shared/global rows such as a
    route usable by any client) stay visible."""
    client_id = client_scope_id(user)
    if client_id is None:
        return qs
    return qs.filter(Q(**{lookup: client_id}) | Q(**{lookup: None}))


def scoped_by_vehicle(qs, user, vehicle_lookup="vehicle"):
    """For models tied to a client only through a vehicle."""
    return scope_queryset(qs, user, f"{vehicle_lookup}__client_id")


def client_scoped(lookup="client_id", include_shared=False):
    """Class decorator for list/detail views: wraps ``get_queryset`` so its
    result is always restricted to the viewer's client. Applied to a list
    view, its subclasses (export views) inherit the scoping automatically;
    applied to a DetailView it makes another client's record a 404.

    ``include_shared`` keeps rows with no client (e.g. a route usable by any
    client) visible to client users.
    """

    def decorator(cls):
        original = cls.get_queryset

        def get_queryset(self):
            qs = original(self)
            if include_shared:
                return scope_queryset_including_shared(qs, self.request.user, lookup)
            return scope_queryset(qs, self.request.user, lookup)

        cls.get_queryset = get_queryset
        return cls

    return decorator
