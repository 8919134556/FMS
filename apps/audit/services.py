from apps.audit.middleware import get_current_request
from apps.audit.models import AuditLog
from apps.core.utils import get_client_ip, get_user_agent


def log_action(
    *,
    action: str,
    module: str,
    entity: str,
    entity_id: str = "",
    old_value: dict | None = None,
    new_value: dict | None = None,
    user=None,
    request=None,
) -> AuditLog:
    """Create an audit trail entry. The one and only way AuditLog rows get created.

    ``user``/``request`` are both optional: if omitted, the current request is
    pulled from thread-local storage (set by ``CurrentRequestMiddleware``) so
    call sites that already have ``request`` in scope can pass it directly,
    while signal handlers that only get a sender/instance still get IP/user
    agent captured automatically.
    """
    request = request or get_current_request()
    if user is None and request is not None:
        user = getattr(request, "user", None)
    if user is not None and not getattr(user, "is_authenticated", True):
        user = None

    return AuditLog.objects.create(
        user=user,
        action=action,
        module=module,
        entity=entity,
        entity_id=str(entity_id),
        old_value=old_value,
        new_value=new_value,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
