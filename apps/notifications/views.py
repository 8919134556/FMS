from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import ListView, TemplateView

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.audit.services import log_action
from apps.core.permissions import ModulePermissionRequiredMixin
from apps.notifications import services
from apps.notifications.forms import BroadcastNotificationForm
from apps.notifications.models import Notification

NOTIFICATION_PAGE_SIZE = 20
BELL_DROPDOWN_LIMIT = 8


class NotificationListView(LoginRequiredMixin, ListView):
    """Every authenticated user's own inbox — no module permission gate,
    same as viewing your own profile. Scoped hard to ``request.user`` so
    there is no way to view or act on anyone else's notifications."""

    model = Notification
    template_name = "notifications/notification_list.html"
    context_object_name = "notifications"
    paginate_by = NOTIFICATION_PAGE_SIZE

    def get_queryset(self):
        qs = Notification.objects.filter(recipient=self.request.user, is_archived=False)
        status = self.request.GET.get("status", "")
        if status == "unread":
            qs = qs.filter(is_read=False)
        elif status == "read":
            qs = qs.filter(is_read=True)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["current_status"] = self.request.GET.get("status", "")
        context["unread_count"] = services.unread_count(self.request.user)
        return context


class NotificationFeedView(LoginRequiredMixin, TemplateView):
    """JSON feed for the navbar bell dropdown — same polling pattern as the
    Live Tracking fleet feed, just much lighter (no map, small page size).

    ``alerts`` lists the user's UNREAD notifications that announce a vehicle
    alert event (apps.alerts.events), with the event's details: the bell shows
    the emergency popup and plays the sound for each new one exactly once (the
    browser remembers which it announced). Nothing here detects alerts."""

    def get(self, request, *args, **kwargs):
        qs = Notification.objects.filter(recipient=request.user, is_archived=False)[:BELL_DROPDOWN_LIMIT]
        results = [
            {
                "uuid": str(n.uuid),
                "title": n.title,
                "body": n.body,
                "level": n.level,
                "link_url": n.link_url,
                "is_read": n.is_read,
                "created_at": n.created_at.isoformat(),
                "is_alert": n.alert_id is not None,
            }
            for n in qs
        ]
        return JsonResponse({
            "unread_count": services.unread_count(request.user),
            "results": results,
            "alerts": services.unread_alert_announcements(request.user),
        })


class NotificationPulseView(LoginRequiredMixin, TemplateView):
    """The bell's fast check (every ~2 s): one indexed lookup — the user's
    newest unread ALERT notification and the unread count — so a new alert
    reaches the popup + sound within seconds, without re-sending the whole
    feed on every tick. The bell loads the full feed only when this changes."""

    def get(self, request, *args, **kwargs):
        latest = (
            Notification.objects.filter(recipient=request.user, is_read=False, is_archived=False,
                                        alert__isnull=False)
            .order_by("-created_at").values_list("uuid", flat=True).first()
        )
        return JsonResponse({"latest_alert": str(latest) if latest else None,
                             "unread_count": services.unread_count(request.user)})


@require_POST
@login_required
def mark_read(request, uuid):
    notification = get_object_or_404(Notification, uuid=uuid, recipient=request.user)
    notification.mark_read()
    if request.headers.get("Accept") == "application/json":
        return JsonResponse({"ok": True, "unread_count": services.unread_count(request.user)})
    next_url = request.POST.get("next") or reverse("notifications:notification_list")
    return redirect(next_url)


@require_POST
@login_required
def mark_all_read(request):
    from django.utils import timezone

    Notification.objects.filter(recipient=request.user, is_read=False).update(is_read=True, read_at=timezone.now())
    if request.headers.get("Accept") == "application/json":
        return JsonResponse({"ok": True, "unread_count": 0})
    next_url = request.POST.get("next") or reverse("notifications:notification_list")
    return redirect(next_url)


@require_POST
@login_required
def archive(request, uuid):
    notification = get_object_or_404(Notification, uuid=uuid, recipient=request.user)
    notification.is_archived = True
    notification.save(update_fields=["is_archived"])
    next_url = request.POST.get("next") or reverse("notifications:notification_list")
    return redirect(next_url)


class BroadcastNotificationView(ModulePermissionRequiredMixin, TemplateView):
    """Admin-only: send a notification to many users at once. This is what
    RBAC's ``notification.create`` permission actually gates — a personal
    inbox needs no permission to read, but composing one for *other* people
    does."""

    template_name = "notifications/broadcast_form.html"
    permission_module = "notification"
    permission_action = "create"

    def get(self, request, *args, **kwargs):
        return self.render_to_response({"form": BroadcastNotificationForm()})

    def post(self, request, *args, **kwargs):
        form = BroadcastNotificationForm(request.POST)
        if form.is_valid():
            if form.cleaned_data["audience"] == BroadcastNotificationForm.Audience.SPECIFIC_ROLE:
                recipients = list(User.objects.filter(role=form.cleaned_data["role"]))
            else:
                recipients = list(User.objects.all())
            services.notify_many(
                recipients,
                title=form.cleaned_data["title"],
                body=form.cleaned_data["body"],
                level=form.cleaned_data["level"],
                link_url=form.cleaned_data["link_url"],
                created_by=request.user,
            )
            log_action(
                action=AuditLog.Action.CREATE, module="notification", entity="Notification", entity_id="",
                new_value={"audience": form.cleaned_data["audience"], "title": form.cleaned_data["title"], "recipient_count": len(recipients)},
                user=request.user, request=request,
            )
            messages.success(request, f"Notification sent to {len(recipients)} user{'s' if len(recipients) != 1 else ''}.")
            return redirect("notifications:broadcast")
        return self.render_to_response({"form": form})
