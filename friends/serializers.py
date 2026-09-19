from rest_framework import serializers
from users.models import User
from .models import FriendRequest, HiRequest, Message


class BasicUserSerializer(serializers.ModelSerializer):
    def to_representation(self, instance):
        data = super().to_representation(instance)
        if instance.deletion_requested_at or instance.retired_account_id:
            data.update(username='Deleted account', profile_picture=None)
        return data

    class Meta:
        model = User
        fields = ('id', 'username', 'profile_picture')


class HiRequestSerializer(serializers.ModelSerializer):
    sender = BasicUserSerializer(read_only=True)
    receiver = BasicUserSerializer(read_only=True)

    class Meta:
        model = HiRequest
        fields = ('id', 'sender', 'receiver', 'status', 'created_at')


class FriendRequestSerializer(serializers.ModelSerializer):
    sender = BasicUserSerializer(read_only=True)
    receiver = BasicUserSerializer(read_only=True)

    class Meta:
        model = FriendRequest
        fields = ('id', 'sender', 'receiver', 'status', 'created_at')


class MessageSerializer(serializers.ModelSerializer):
    sender = BasicUserSerializer(read_only=True)
    # Kept under the old key: clients read `image` whether the file went
    # through Django or (for messages from before the R2 migration) straight to
    # Cloudinary.
    image = serializers.SerializerMethodField()
    swift = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = (
            'id', 'client_id', 'sender', 'content', 'image', 'swift',
            'created_at', 'is_read',
        )

    def get_image(self, obj):
        if obj.image_url:
            return obj.image_url
        return obj.image.url if obj.image else None

    def get_swift(self, obj):
        """The card for a shared Swift, or None if this reader may not watch it.

        Re-checked on every read rather than trusted from send time, and that
        is the whole point of storing a foreign key instead of a link. Between
        the share and this request the Swift can be deleted, taken down,
        switched to private, or its author can block the reader — and in every
        one of those cases the card has to stop rendering. A URL in `content`
        would keep working.

        The set comes from the view, which computes it once for the page:
        asking `watchable` per message would be a query per row on a screen
        that loads fifty at a time. A caller that does not supply it gets None,
        which fails closed — an unrendered card is a far better bug than a
        leaked one.
        """
        if not obj.swift_id:
            return None
        watchable = self.context.get('shareable_ids')
        if not watchable or obj.swift_id not in watchable:
            return None

        swift = obj.swift
        from swifts import media as swift_media

        return {
            'id': swift.pk,
            'caption': swift.caption,
            'thumbnail_url': swift_media.poster_url(swift),
            'video_url': swift_media.playback_url(swift),
            'duration_ms': swift.duration_ms,
            'author': {
                'id': swift.author_id,
                'username': swift.author.username,
            },
        }


class MessageHistoryQuerySerializer(serializers.Serializer):
    before = serializers.IntegerField(required=False, min_value=1, max_value=2**63 - 1)
    limit = serializers.IntegerField(default=50, min_value=1, max_value=100)


class MessageReadSerializer(serializers.Serializer):
    through_id = serializers.IntegerField(min_value=1, max_value=2**63 - 1)
