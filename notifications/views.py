from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db.models import Q
from django.shortcuts import get_object_or_404
from users.visibility import blocked_ids_for, proximity_visible_ids
from .models import Notification
from .serializers import NotificationSerializer


class NotificationListView(generics.ListAPIView):
    serializer_class = NotificationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = Notification.objects.filter(recipient=user).select_related(
            'sender', 'story', 'club', 'event',
        )

        # Filtered on read, not only on write: a notification names its sender,
        # so blocking someone has to clear the ones already sitting in the list.
        #
        # Which filter depends on the type. A proximity alert exists only
        # because of where someone was, so it also has to answer to the
        # location-sharing and interest-discovery settings as they stand now —
        # someone who has since paused should stop appearing. A story alert is
        # about something the sender deliberately did to content the recipient
        # published; running it through the proximity rules would hide "@miras
        # liked your story" from you because Miras turned off interest
        # discovery. Blocks are the only gate that belongs on those.
        proximity_types = [Notification.Type.NEARBY_USER]
        sender_ids = [
            pk for pk in qs.filter(notification_type__in=proximity_types)
                           .values_list('sender_id', flat=True)
            if pk
        ]
        allowed = proximity_visible_ids(user, sender_ids)
        blocked = blocked_ids_for(user)

        return qs.filter(
            Q(sender__isnull=True)
            | Q(notification_type__in=proximity_types, sender_id__in=allowed)
            | ~Q(notification_type__in=proximity_types) & ~Q(sender_id__in=blocked)
        )


class MarkNotificationReadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, pk):
        notification = get_object_or_404(Notification, pk=pk, recipient=request.user)
        notification.is_read = True
        notification.save(update_fields=['is_read'])
        return Response(status=status.HTTP_204_NO_CONTENT)


class MarkAllReadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request):
        Notification.objects.filter(recipient=request.user, is_read=False).update(is_read=True)
        return Response(status=status.HTTP_204_NO_CONTENT)

    def post(self, request):
        Notification.objects.filter(recipient=request.user, is_read=False).update(is_read=True)
        return Response(status=status.HTTP_204_NO_CONTENT)
