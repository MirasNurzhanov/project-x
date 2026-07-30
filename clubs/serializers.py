from rest_framework import serializers
from users.models import User
from .models import Club, ClubEvent, ClubMember, ClubMessage


class BasicUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ('id', 'username', 'profile_picture')


class ClubMemberSerializer(serializers.ModelSerializer):
    user = BasicUserSerializer(read_only=True)

    class Meta:
        model = ClubMember
        fields = ('id', 'user', 'role', 'joined_at')


class ClubSerializer(serializers.ModelSerializer):
    member_count = serializers.IntegerField(read_only=True)
    my_role = serializers.SerializerMethodField()
    creator = BasicUserSerializer(read_only=True)

    class Meta:
        model = Club
        fields = ('id', 'name', 'description', 'interest', 'emblem', 'creator', 'created_at', 'is_active', 'member_count', 'my_role')

    def get_my_role(self, obj):
        request = self.context.get('request')
        if not request:
            return None
        try:
            return obj.members.get(user=request.user).role
        except ClubMember.DoesNotExist:
            return None


class ClubDetailSerializer(ClubSerializer):
    members = ClubMemberSerializer(many=True, read_only=True)

    class Meta(ClubSerializer.Meta):
        fields = ClubSerializer.Meta.fields + ('members',)


class ClubEventSerializer(serializers.ModelSerializer):
    created_by = BasicUserSerializer(read_only=True)

    class Meta:
        model = ClubEvent
        fields = ('id', 'club', 'created_by', 'title', 'description', 'location_name', 'latitude', 'longitude', 'event_datetime', 'created_at')


class ClubMessageSerializer(serializers.ModelSerializer):
    sender = BasicUserSerializer(read_only=True)

    class Meta:
        model = ClubMessage
        fields = ('id', 'club', 'sender', 'content', 'image', 'message_type', 'created_at')
