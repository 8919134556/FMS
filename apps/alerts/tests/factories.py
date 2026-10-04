import factory
from factory.django import DjangoModelFactory

from apps.alerts.models import Alert


class AlertFactory(DjangoModelFactory):
    class Meta:
        model = Alert

    dedupe_key = factory.Sequence(lambda n: f"TEST_KEY:{n}")
    category = Alert.Category.TRIP_DELAYED
    severity = Alert.Severity.HIGH
    status = Alert.Status.OPEN
    title = factory.Sequence(lambda n: f"Alert {n}")
