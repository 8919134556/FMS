import datetime
import threading

import pytest
from django.db import connection, transaction
from django.utils import timezone

from apps.tracking.models import DeviceCommand
from apps.tracking.telematics_service import command_dispatch
from apps.tracking.tests.factories import DeviceCommandFactory, TrackingDeviceFactory


@pytest.mark.django_db
class TestClaimAndQueue:
    def test_claims_pending_for_connected_devices_only(self):
        device_a = TrackingDeviceFactory()
        device_b = TrackingDeviceFactory()
        command_a = DeviceCommandFactory(device=device_a, status=DeviceCommand.Status.PENDING)
        DeviceCommandFactory(device=device_b, status=DeviceCommand.Status.PENDING)  # not connected

        claimed = command_dispatch._claim_and_queue_sync([device_a.pk])
        assert [c.pk for c in claimed] == [command_a.pk]
        command_a.refresh_from_db()
        assert command_a.status == DeviceCommand.Status.QUEUED

    def test_empty_device_list_claims_nothing(self):
        assert command_dispatch._claim_and_queue_sync([]) == []

    def test_second_claim_before_dispatch_is_idempotent(self):
        """An already-QUEUED row must never be claimed again."""
        device = TrackingDeviceFactory()
        command = DeviceCommandFactory(device=device, status=DeviceCommand.Status.PENDING)

        first = command_dispatch._claim_and_queue_sync([device.pk])
        second = command_dispatch._claim_and_queue_sync([device.pk])

        assert [c.pk for c in first] == [command.pk]
        assert second == []

    def test_claims_in_created_order(self):
        device = TrackingDeviceFactory()
        older = DeviceCommandFactory(device=device, status=DeviceCommand.Status.PENDING)
        DeviceCommand.objects.filter(pk=older.pk).update(created_at=timezone.now() - datetime.timedelta(minutes=5))
        newer = DeviceCommandFactory(device=device, status=DeviceCommand.Status.PENDING)

        claimed = command_dispatch._claim_and_queue_sync([device.pk])
        assert [c.pk for c in claimed] == [older.pk, newer.pk]


@pytest.mark.django_db
class TestReleaseStuckQueued:
    def test_queued_reverts_to_pending_on_startup(self):
        command = DeviceCommandFactory(status=DeviceCommand.Status.QUEUED)
        command_dispatch._release_stuck_queued_sync()
        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.PENDING

    def test_other_statuses_untouched(self):
        pending = DeviceCommandFactory(status=DeviceCommand.Status.PENDING)
        sent = DeviceCommandFactory(status=DeviceCommand.Status.SENT)
        command_dispatch._release_stuck_queued_sync()
        pending.refresh_from_db()
        sent.refresh_from_db()
        assert pending.status == DeviceCommand.Status.PENDING
        assert sent.status == DeviceCommand.Status.SENT


@pytest.mark.django_db
class TestSweepTimeoutsAndExpiry:
    def test_sent_past_ack_timeout_retries_when_under_cap(self, settings):
        settings.TELEMATICS_COMMAND_ACK_TIMEOUT_SECONDS = 60
        command = DeviceCommandFactory(
            status=DeviceCommand.Status.SENT, retry_count=0, max_retries=3,
            sent_at=timezone.now() - datetime.timedelta(seconds=120),
        )
        command_dispatch._sweep_timeouts_and_expiry_sync()
        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.PENDING
        assert command.retry_count == 1
        assert "timeout" in command.last_error.lower()

    def test_sent_past_ack_timeout_expires_when_at_cap(self, settings):
        settings.TELEMATICS_COMMAND_ACK_TIMEOUT_SECONDS = 60
        command = DeviceCommandFactory(
            status=DeviceCommand.Status.SENT, retry_count=3, max_retries=3,
            sent_at=timezone.now() - datetime.timedelta(seconds=120),
        )
        command_dispatch._sweep_timeouts_and_expiry_sync()
        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.EXPIRED

    def test_sent_within_timeout_untouched(self, settings):
        settings.TELEMATICS_COMMAND_ACK_TIMEOUT_SECONDS = 60
        command = DeviceCommandFactory(status=DeviceCommand.Status.SENT, sent_at=timezone.now())
        command_dispatch._sweep_timeouts_and_expiry_sync()
        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.SENT

    def test_expires_at_deadline_expires_pending_regardless_of_retries(self):
        command = DeviceCommandFactory(
            status=DeviceCommand.Status.PENDING,
            expires_at=timezone.now() - datetime.timedelta(minutes=1),
        )
        command_dispatch._sweep_timeouts_and_expiry_sync()
        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.EXPIRED

    def test_expires_at_deadline_expires_queued(self):
        command = DeviceCommandFactory(
            status=DeviceCommand.Status.QUEUED,
            expires_at=timezone.now() - datetime.timedelta(minutes=1),
        )
        command_dispatch._sweep_timeouts_and_expiry_sync()
        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.EXPIRED

    def test_future_expires_at_untouched(self):
        command = DeviceCommandFactory(
            status=DeviceCommand.Status.PENDING,
            expires_at=timezone.now() + datetime.timedelta(hours=1),
        )
        command_dispatch._sweep_timeouts_and_expiry_sync()
        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.PENDING


class TestDatabaseSafeClaiming:
    @pytest.mark.django_db(transaction=True)
    def test_skip_locked_prevents_double_claim_under_real_concurrency(self):
        """A genuine two-connection concurrency test: one thread holds a
        real row lock on the PENDING command; a concurrent claimer must
        skip it entirely (empty result), never block-then-double-claim."""
        device = TrackingDeviceFactory()
        command = DeviceCommandFactory(device=device, status=DeviceCommand.Status.PENDING)

        lock_acquired = threading.Event()
        release_lock = threading.Event()

        def hold_lock():
            with transaction.atomic():
                DeviceCommand.objects.select_for_update().filter(pk=command.pk).first()
                lock_acquired.set()
                release_lock.wait(timeout=5)
            connection.close()

        holder_thread = threading.Thread(target=hold_lock)
        holder_thread.start()
        try:
            assert lock_acquired.wait(timeout=5)
            claimed = command_dispatch._claim_and_queue_sync([device.pk])
            assert claimed == []  # skipped, not blocked-then-claimed
        finally:
            release_lock.set()
            holder_thread.join(timeout=5)

        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.PENDING  # never claimed

        # Once the lock is released, a normal claim succeeds exactly once.
        claimed_after = command_dispatch._claim_and_queue_sync([device.pk])
        assert [c.pk for c in claimed_after] == [command.pk]
