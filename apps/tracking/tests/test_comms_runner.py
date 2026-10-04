"""Auto-started comms feed: the background thread, its start-up conditions and
the advisory lock that keeps concurrent syncers from overlapping."""

import sys
import threading

import pytest
from django.apps import apps
from django.db import connections

from apps.tracking import comms_runner, comms_sync

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _clean_runner():
    comms_runner.stop_background_sync()
    comms_runner._thread = None
    yield
    comms_runner.stop_background_sync()
    comms_runner._thread = None


@pytest.fixture
def enabled(settings):
    settings.COMMS_SYNC_AUTOSTART = True
    settings.COMMS_APP_DATABASE_URL = "postgres://user:pw@localhost:5432/APPDB"
    settings.COMMS_SYNC_INTERVAL_SECONDS = 1
    return settings


class TestEnablement:
    def test_off_by_default_and_without_a_comms_database(self, settings):
        settings.COMMS_SYNC_AUTOSTART = False
        settings.COMMS_APP_DATABASE_URL = "postgres://x/y"
        assert comms_runner.is_enabled() is False
        settings.COMMS_SYNC_AUTOSTART = True
        settings.COMMS_APP_DATABASE_URL = ""
        assert comms_runner.is_enabled() is False
        assert comms_runner.start_background_sync() is False

    def test_starts_one_daemon_thread_and_is_idempotent(self, enabled, monkeypatch):
        release = threading.Event()
        monkeypatch.setattr(comms_runner, "_run", lambda: release.wait(5))
        assert comms_runner.start_background_sync() is True
        assert comms_runner.start_background_sync() is False  # already running
        thread = comms_runner._thread
        assert thread.daemon and thread.name == "comms-sync"
        release.set()


class TestLoop:
    def test_keeps_syncing_and_survives_a_failing_pass(self, enabled, monkeypatch):
        monkeypatch.setattr(comms_runner, "STARTUP_DELAY_SECONDS", 0)
        calls = []
        two_calls = threading.Event()

        def fake_sync_exclusive():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("comms db is restarting")
            if len(calls) >= 2:
                two_calls.set()

        monkeypatch.setattr(comms_sync, "sync_exclusive", fake_sync_exclusive)
        assert comms_runner.start_background_sync()
        assert two_calls.wait(10), "the loop did not retry after a failed pass"
        comms_runner.stop_background_sync()
        assert not comms_runner._thread.is_alive()


class TestAppConfigStartup:
    def _ready(self, monkeypatch, argv, run_main=None):
        started = []
        monkeypatch.setattr(comms_runner, "start_background_sync", lambda: started.append(1))
        monkeypatch.setattr(sys, "argv", argv)
        if run_main is None:
            monkeypatch.delenv("RUN_MAIN", raising=False)
        else:
            monkeypatch.setenv("RUN_MAIN", run_main)
        apps.get_app_config("tracking").ready()
        return started

    def test_runserver_reloader_child_starts_it(self, enabled, monkeypatch):
        assert self._ready(monkeypatch, ["manage.py", "runserver"], run_main="true") == [1]

    def test_reloader_watcher_process_does_not(self, enabled, monkeypatch):
        assert self._ready(monkeypatch, ["manage.py", "runserver"]) == []

    def test_noreload_runserver_starts_it(self, enabled, monkeypatch):
        assert self._ready(monkeypatch, ["manage.py", "runserver", "--noreload"]) == [1]

    def test_other_commands_and_tests_never_start_it(self, enabled, monkeypatch):
        for argv in (["manage.py", "migrate"], ["manage.py", "shell"], ["pytest"], ["manage.py", "sync_comms_data"]):
            assert self._ready(monkeypatch, argv, run_main="true") == []

    def test_disabled_setting_never_starts_it(self, settings, monkeypatch):
        settings.COMMS_SYNC_AUTOSTART = False
        assert self._ready(monkeypatch, ["manage.py", "runserver", "--noreload"]) == []


class TestAdvisoryLock:
    def test_a_pass_is_skipped_while_another_session_holds_the_lock(self, monkeypatch):
        other = connections["default"].copy()
        try:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s)", [comms_sync.ADVISORY_LOCK_KEY])
            monkeypatch.setattr(comms_sync, "sync", lambda **kw: pytest.fail("must not sync while locked"))
            assert comms_sync.sync_exclusive() is None
        finally:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [comms_sync.ADVISORY_LOCK_KEY])
            other.close()

    def test_runs_and_releases_the_lock_when_free(self, monkeypatch):
        sentinel = object()
        monkeypatch.setattr(comms_sync, "sync", lambda **kw: sentinel)
        assert comms_sync.sync_exclusive() is sentinel
        # released: a second, independent session can take it immediately
        other = connections["default"].copy()
        try:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", [comms_sync.ADVISORY_LOCK_KEY])
                assert cursor.fetchone()[0] is True
                cursor.execute("SELECT pg_advisory_unlock(%s)", [comms_sync.ADVISORY_LOCK_KEY])
        finally:
            other.close()

    def test_lock_is_released_even_if_the_sync_raises(self, monkeypatch):
        def boom(**kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(comms_sync, "sync", boom)
        with pytest.raises(RuntimeError):
            comms_sync.sync_exclusive()
        other = connections["default"].copy()
        try:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", [comms_sync.ADVISORY_LOCK_KEY])
                assert cursor.fetchone()[0] is True
                cursor.execute("SELECT pg_advisory_unlock(%s)", [comms_sync.ADVISORY_LOCK_KEY])
        finally:
            other.close()
