import datetime

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.maintenance.models import Maintenance
from apps.maintenance.tests.factories import MaintenanceFactory, MaintenancePartFactory

pytestmark = pytest.mark.django_db


class TestMaintenanceUniqueness:
    def test_duplicate_maintenance_number_rejected(self):
        MaintenanceFactory(maintenance_number="MNT-999999")
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                MaintenanceFactory(maintenance_number="MNT-999999")


class TestOverdueLogic:
    def test_future_scheduled_date_not_overdue(self):
        m = MaintenanceFactory(scheduled_date=timezone.now().date() + datetime.timedelta(days=Maintenance.DUE_SOON_WINDOW_DAYS + 5))
        assert m.is_overdue is False
        assert m.overdue_days == 0
        assert m.urgency == "NORMAL"

    def test_past_scheduled_date_is_overdue(self):
        m = MaintenanceFactory(scheduled_date=timezone.now().date() - datetime.timedelta(days=3))
        assert m.is_overdue is True
        assert m.overdue_days == 3
        assert m.urgency == "OVERDUE"
        assert m.overdue_label == "3 days overdue"

    def test_singular_overdue_label(self):
        m = MaintenanceFactory(scheduled_date=timezone.now().date() - datetime.timedelta(days=1))
        assert m.overdue_label == "1 day overdue"

    def test_today_is_due_today_not_overdue(self):
        m = MaintenanceFactory(scheduled_date=timezone.now().date())
        assert m.is_overdue is False
        assert m.urgency == "DUE_TODAY"

    def test_within_window_is_due_soon(self):
        m = MaintenanceFactory(scheduled_date=timezone.now().date() + datetime.timedelta(days=Maintenance.DUE_SOON_WINDOW_DAYS))
        assert m.urgency == "DUE_SOON"

    def test_completed_is_never_overdue(self):
        m = MaintenanceFactory(
            status=Maintenance.Status.COMPLETED, scheduled_date=timezone.now().date() - datetime.timedelta(days=10)
        )
        assert m.is_overdue is False
        assert m.urgency is None

    def test_cancelled_is_never_overdue(self):
        m = MaintenanceFactory(
            status=Maintenance.Status.CANCELLED, scheduled_date=timezone.now().date() - datetime.timedelta(days=10)
        )
        assert m.is_overdue is False
        assert m.urgency is None


class TestMaintenanceQuerySet:
    def test_overdue_queryset(self):
        overdue = MaintenanceFactory(scheduled_date=timezone.now().date() - datetime.timedelta(days=1))
        MaintenanceFactory(scheduled_date=timezone.now().date() + datetime.timedelta(days=1))
        results = Maintenance.objects.overdue()
        assert overdue in results
        assert results.count() == 1

    def test_due_today_queryset(self):
        due_today = MaintenanceFactory(scheduled_date=timezone.now().date())
        MaintenanceFactory(scheduled_date=timezone.now().date() + datetime.timedelta(days=1))
        results = Maintenance.objects.due_today()
        assert due_today in results
        assert results.count() == 1

    def test_due_soon_queryset(self):
        due_soon = MaintenanceFactory(scheduled_date=timezone.now().date() + datetime.timedelta(days=5))
        MaintenanceFactory(scheduled_date=timezone.now().date() + datetime.timedelta(days=20))
        results = Maintenance.objects.due_soon()
        assert due_soon in results
        assert results.count() == 1

    def test_in_progress_excluded_from_overdue(self):
        MaintenanceFactory(
            status=Maintenance.Status.IN_PROGRESS, scheduled_date=timezone.now().date() - datetime.timedelta(days=5)
        )
        assert Maintenance.objects.overdue().count() == 0


class TestStatusTransitions:
    def test_scheduled_can_go_to_in_progress(self):
        m = MaintenanceFactory(status=Maintenance.Status.SCHEDULED)
        assert m.can_transition_to(Maintenance.Status.IN_PROGRESS) is True

    def test_scheduled_can_be_cancelled(self):
        m = MaintenanceFactory(status=Maintenance.Status.SCHEDULED)
        assert m.can_transition_to(Maintenance.Status.CANCELLED) is True

    def test_completed_is_terminal(self):
        m = MaintenanceFactory(status=Maintenance.Status.COMPLETED)
        assert m.VALID_TRANSITIONS[Maintenance.Status.COMPLETED] == set()

    def test_cancelled_is_terminal(self):
        m = MaintenanceFactory(status=Maintenance.Status.CANCELLED)
        assert m.VALID_TRANSITIONS[Maintenance.Status.CANCELLED] == set()

    def test_in_progress_cannot_go_back_to_scheduled(self):
        m = MaintenanceFactory(status=Maintenance.Status.IN_PROGRESS)
        assert m.can_transition_to(Maintenance.Status.SCHEDULED) is False


class TestCostCalculations:
    def test_parts_cost_sums_line_items(self):
        m = MaintenanceFactory()
        MaintenancePartFactory(maintenance=m, quantity=2, unit_cost=50)
        MaintenancePartFactory(maintenance=m, quantity=1, unit_cost=25)
        assert m.parts_cost == 125

    def test_parts_cost_zero_when_no_parts(self):
        m = MaintenanceFactory()
        assert m.parts_cost == 0

    def test_part_total_cost(self):
        part = MaintenancePartFactory(quantity=3, unit_cost=10)
        assert part.total_cost == 30

    def test_cost_variance_positive_when_over_budget(self):
        m = MaintenanceFactory(estimated_cost=1000, actual_cost=1200)
        assert m.cost_variance == 200

    def test_cost_variance_none_without_both_costs(self):
        m = MaintenanceFactory(estimated_cost=1000, actual_cost=None)
        assert m.cost_variance is None


class TestDuration:
    def test_duration_none_without_both_dates(self):
        m = MaintenanceFactory()
        assert m.duration is None

    def test_duration_calculated(self):
        start = timezone.now()
        end = start + datetime.timedelta(hours=3)
        m = MaintenanceFactory(started_date=start, completed_date=end)
        assert m.duration == datetime.timedelta(hours=3)
