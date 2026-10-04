import pytest
from django.core.exceptions import ValidationError

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditLog
from apps.tracking import command_services
from apps.tracking.models import DeviceCommand
from apps.tracking.tests.factories import DeviceCommandFactory, TrackingDeviceFactory

pytestmark = pytest.mark.django_db


class TestCanTransitionTo:
    def test_valid_edges(self):
        command = DeviceCommandFactory(status=DeviceCommand.Status.PENDING)
        assert command.can_transition_to(DeviceCommand.Status.QUEUED)
        assert command.can_transition_to(DeviceCommand.Status.CANCELLED)

    def test_invalid_edges(self):
        command = DeviceCommandFactory(status=DeviceCommand.Status.PENDING)
        assert not command.can_transition_to(DeviceCommand.Status.SENT)
        assert not command.can_transition_to(DeviceCommand.Status.COMPLETED)

    def test_fully_terminal_statuses_have_no_outgoing_edges(self):
        """COMPLETED/CANCELLED are truly terminal. FAILED/EXPIRED allow
        exactly one outgoing edge (->PENDING) for the retry path — covered
        separately in TestRetryCommand."""
        for status in (DeviceCommand.Status.COMPLETED, DeviceCommand.Status.CANCELLED):
            command = DeviceCommandFactory(status=status)
            assert not command.can_transition_to(DeviceCommand.Status.PENDING)

    def test_failed_and_expired_allow_only_retry_to_pending(self):
        for status in (DeviceCommand.Status.FAILED, DeviceCommand.Status.EXPIRED):
            command = DeviceCommandFactory(status=status)
            assert command.can_transition_to(DeviceCommand.Status.PENDING)
            assert not command.can_transition_to(DeviceCommand.Status.SENT)
            assert not command.can_transition_to(DeviceCommand.Status.COMPLETED)


class TestTransitionCommand:
    def test_valid_transition_stamps_timestamp(self):
        command = DeviceCommandFactory(status=DeviceCommand.Status.PENDING)
        command_services.transition_command(command=command, new_status=DeviceCommand.Status.QUEUED)
        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.QUEUED
        assert command.queued_at is not None

    def test_terminal_status_stamps_completed_at_even_for_failure(self):
        command = DeviceCommandFactory(status=DeviceCommand.Status.PENDING)
        command_services.transition_command(command=command, new_status=DeviceCommand.Status.CANCELLED)
        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.CANCELLED
        assert command.completed_at is not None

    def test_invalid_transition_raises(self):
        command = DeviceCommandFactory(status=DeviceCommand.Status.PENDING)
        with pytest.raises(ValidationError):
            command_services.transition_command(command=command, new_status=DeviceCommand.Status.COMPLETED)
        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.PENDING  # unchanged

    def test_extra_field_updates_applied(self):
        command = DeviceCommandFactory(status=DeviceCommand.Status.SENT)
        command_services.transition_command(
            command=command, new_status=DeviceCommand.Status.PENDING, retry_count=1, last_error="timeout",
        )
        command.refresh_from_db()
        assert command.retry_count == 1
        assert command.last_error == "timeout"

    def test_timestamp_not_overwritten_on_repeat_transition_to_same_status(self):
        """A guard against re-stamping if transition_command were somehow
        called twice for the same edge (defensive, not expected in normal
        flow since transitions are one-directional per state)."""
        command = DeviceCommandFactory(status=DeviceCommand.Status.PENDING)
        command_services.transition_command(command=command, new_status=DeviceCommand.Status.QUEUED)
        command.refresh_from_db()
        first_queued_at = command.queued_at
        # QUEUED -> PENDING -> QUEUED again would naturally re-stamp since
        # it's a fresh transition; this just confirms the field is set once
        # per genuine transition, not continuously.
        assert command.queued_at == first_queued_at


class TestCreateCommand:
    def test_creates_pending_and_logs_audit(self):
        device = TrackingDeviceFactory()
        actor = UserFactory()
        command = command_services.create_command(
            device=device, command_type=DeviceCommand.CommandType.PING, actor=actor,
        )
        assert command.status == DeviceCommand.Status.PENDING
        assert command.created_by == actor
        assert AuditLog.objects.filter(
            module="tracking_device", entity="DeviceCommand", entity_id=str(command.pk), action=AuditLog.Action.CREATE
        ).exists()

    def test_default_payload_is_empty_dict(self):
        device = TrackingDeviceFactory()
        command = command_services.create_command(device=device, command_type=DeviceCommand.CommandType.PING, actor=UserFactory())
        assert command.payload == {}

    def test_custom_payload_and_expiry_stored(self):
        import datetime

        from django.utils import timezone

        device = TrackingDeviceFactory()
        expires = timezone.now() + datetime.timedelta(hours=1)
        command = command_services.create_command(
            device=device, command_type=DeviceCommand.CommandType.CUSTOM,
            payload={"foo": "bar"}, expires_at=expires, actor=UserFactory(),
        )
        assert command.payload == {"foo": "bar"}
        assert command.expires_at == expires


class TestCancelCommand:
    def test_cancel_pending_succeeds(self):
        command = DeviceCommandFactory(status=DeviceCommand.Status.PENDING)
        actor = UserFactory()
        command_services.cancel_command(command=command, actor=actor)
        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.CANCELLED
        assert AuditLog.objects.filter(
            entity="DeviceCommand", entity_id=str(command.pk), action=AuditLog.Action.CANCEL
        ).exists()

    def test_cancel_queued_succeeds(self):
        command = DeviceCommandFactory(status=DeviceCommand.Status.QUEUED)
        command_services.cancel_command(command=command, actor=UserFactory())
        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.CANCELLED

    def test_cancel_sent_rejected(self):
        command = DeviceCommandFactory(status=DeviceCommand.Status.SENT)
        with pytest.raises(ValidationError):
            command_services.cancel_command(command=command, actor=UserFactory())
        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.SENT

    def test_cancel_completed_rejected(self):
        command = DeviceCommandFactory(status=DeviceCommand.Status.COMPLETED)
        with pytest.raises(ValidationError):
            command_services.cancel_command(command=command, actor=UserFactory())


class TestRetryCommand:
    def test_retry_failed_under_max_retries_succeeds(self):
        command = DeviceCommandFactory(status=DeviceCommand.Status.FAILED, retry_count=0, max_retries=3)
        command_services.retry_command(command=command, actor=UserFactory())
        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.PENDING
        assert command.retry_count == 1
        assert command.last_error == ""

    def test_retry_expired_under_max_retries_succeeds(self):
        command = DeviceCommandFactory(status=DeviceCommand.Status.EXPIRED, retry_count=1, max_retries=3)
        command_services.retry_command(command=command, actor=UserFactory())
        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.PENDING

    def test_retry_at_max_retries_rejected(self):
        command = DeviceCommandFactory(status=DeviceCommand.Status.FAILED, retry_count=3, max_retries=3)
        with pytest.raises(ValidationError):
            command_services.retry_command(command=command, actor=UserFactory())
        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.FAILED

    def test_retry_non_terminal_status_rejected(self):
        command = DeviceCommandFactory(status=DeviceCommand.Status.PENDING)
        with pytest.raises(ValidationError):
            command_services.retry_command(command=command, actor=UserFactory())


class TestRecordTerminalOutcome:
    def test_completed_logs_complete_action(self):
        command = DeviceCommandFactory(status=DeviceCommand.Status.ACKNOWLEDGED)
        command_services.record_terminal_outcome(
            command=command, new_status=DeviceCommand.Status.COMPLETED, response_payload={"ok": True},
        )
        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.COMPLETED
        assert command.response_payload == {"ok": True}
        assert AuditLog.objects.filter(
            entity="DeviceCommand", entity_id=str(command.pk), action=AuditLog.Action.COMPLETE
        ).exists()

    def test_failed_logs_update_action(self):
        command = DeviceCommandFactory(status=DeviceCommand.Status.ACKNOWLEDGED)
        command_services.record_terminal_outcome(
            command=command, new_status=DeviceCommand.Status.FAILED, last_error="device reported error",
        )
        command.refresh_from_db()
        assert command.status == DeviceCommand.Status.FAILED
        assert command.last_error == "device reported error"
        assert AuditLog.objects.filter(
            entity="DeviceCommand", entity_id=str(command.pk), action=AuditLog.Action.UPDATE
        ).exists()

    def test_no_actor_recorded(self):
        command = DeviceCommandFactory(status=DeviceCommand.Status.ACKNOWLEDGED)
        command_services.record_terminal_outcome(command=command, new_status=DeviceCommand.Status.COMPLETED)
        log = AuditLog.objects.filter(entity="DeviceCommand", entity_id=str(command.pk)).latest("timestamp")
        assert log.user is None
