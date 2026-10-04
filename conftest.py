import pytest

from apps.accounts.tests.factories import RoleFactory, UserFactory


@pytest.fixture(autouse=True)
def _ensure_system_settings_row(request):
    """SystemSettings.load() runs on every page via the site_meta context
    processor. In production the row always exists (seeded by migration),
    but transactional tests flush tables, so without this the first request
    in a test lazily INSERTs it and skews query-count assertions that
    compare two requests within one test."""
    marker = request.node.get_closest_marker("django_db")
    if marker is None or marker.kwargs.get("transaction"):
        return
    request.getfixturevalue("db")
    from apps.core.models import SystemSettings

    SystemSettings.load()


@pytest.fixture
def admin_user(db):
    """Overrides pytest-django's built-in admin_user: our User model has extra
    required-in-practice fields (employee_id, mobile_number, ...) that the
    stock fixture doesn't know how to fill in."""
    return UserFactory(
        username="admin",
        email="admin@example.com",
        is_staff=True,
        is_superuser=True,
    )


@pytest.fixture
def role_factory(db):
    return RoleFactory


@pytest.fixture
def user_factory(db):
    return UserFactory
