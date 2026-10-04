import threading

_thread_locals = threading.local()


class CurrentRequestMiddleware:
    """Stashes the in-flight request in thread-local storage.

    Model-level signal handlers (login/logout, and future auto-audit hooks in
    other apps) don't receive the request directly. This lets
    ``apps.audit.services.log_action`` fall back to the current request's
    user/IP/user-agent when the caller doesn't pass one explicitly.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        _thread_locals.request = request
        try:
            return self.get_response(request)
        finally:
            _thread_locals.request = None


def get_current_request():
    return getattr(_thread_locals, "request", None)
