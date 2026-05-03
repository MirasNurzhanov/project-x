from rest_framework import serializers
from .models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    sender_username = serializers.CharField(source='sender.username', read_only=True, allow_null=True)

    class Meta:
        model = Notification
        fields = ('id', 'sender_username', 'notification_type', 'message', 'is_read', 'created_at')
        read_only_fields = ('id', 'sender_username', 'notification_type', 'message', 'created_at')
