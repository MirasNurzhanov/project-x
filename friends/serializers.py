from rest_framework import serializers
from users.models import User
from .models import FriendRequest, HiRequest, Message


class BasicUserSerializer(serializers.ModelSerializer):
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
        fields = ('id', 'sender', 'content', 'image', 'created_at', 'is_read')

    def get_image(self, obj):
        if obj.image_url:
            return obj.image_url
        return obj.image.url if obj.image else None
