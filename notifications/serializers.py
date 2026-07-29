from rest_framework import serializers
from .models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    # `sender_username` predates the rest and keeps its name: older clients read it.
    sender_username = serializers.CharField(source='sender.username', read_only=True, allow_null=True)
    sender_id = serializers.IntegerField(source='sender.id', read_only=True, allow_null=True)
    sender_avatar = serializers.SerializerMethodField()
    story_id = serializers.IntegerField(source='story.id', read_only=True, allow_null=True)
    story_image = serializers.SerializerMethodField()
    story_excerpt = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = (
            'id', 'sender_id', 'sender_username', 'sender_avatar',
            'story_id', 'story_image', 'story_excerpt',
            'notification_type', 'message', 'is_read', 'created_at',
        )
        read_only_fields = fields

    def _absolute(self, image):
        """Image URLs are relative under the default storage; the app needs a
        fetchable one, and the request is the only thing that knows the host."""
        if not image:
            return None
        request = self.context.get('request')
        return request.build_absolute_uri(image.url) if request else image.url

    def get_sender_avatar(self, obj):
        return self._absolute(obj.sender.profile_picture if obj.sender else None)

    def get_story_image(self, obj):
        return self._absolute(obj.story.image if obj.story else None)

    def get_story_excerpt(self, obj):
        """Enough of the story to recognise which one an alert is about."""
        story = obj.story
        if not story or not story.content:
            return None
        return story.content if len(story.content) <= 60 else story.content[:60].rstrip() + '…'
