from rest_framework import serializers
from users.models import User
from .models import HiRequest, Message


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


class MessageSerializer(serializers.ModelSerializer):
    sender = BasicUserSerializer(read_only=True)

    class Meta:
        model = Message
        fields = ('id', 'sender', 'content', 'created_at', 'is_read')
