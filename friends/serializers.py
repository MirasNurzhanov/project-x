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

    class Meta:
        model = Message
        fields = ('id', 'client_id', 'sender', 'content', 'image', 'created_at', 'is_read')

    def get_image(self, obj):
        if obj.image_url:
            return obj.image_url
        return obj.image.url if obj.image else None


class MessageHistoryQuerySerializer(serializers.Serializer):
    before = serializers.IntegerField(required=False, min_value=1, max_value=2**63 - 1)
    limit = serializers.IntegerField(default=50, min_value=1, max_value=100)


class MessageReadSerializer(serializers.Serializer):
    through_id = serializers.IntegerField(min_value=1, max_value=2**63 - 1)
