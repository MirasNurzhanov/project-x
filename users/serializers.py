from datetime import date
from django.db.models import Q
from rest_framework import serializers
from django.contrib.auth.password_validation import validate_password
from friends.models import FriendRequest, HiRequest
from .models import User
from .presets import PRESET_MAP, ZONE_PRESETS
from interests.models import Interest
from interests.serializers import InterestSerializer


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, validators=[validate_password])
    password2 = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ('username', 'email', 'password', 'password2', 'date_of_birth')

    def validate(self, attrs):
        if attrs['password'] != attrs['password2']:
            raise serializers.ValidationError({'password': 'Passwords do not match.'})

        dob = attrs.get('date_of_birth')
        if dob:
            today = date.today()
            age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
            if age < 13:
                raise serializers.ValidationError({'date_of_birth': 'You must be at least 13 to register.'})
        return attrs

    def create(self, validated_data):
        validated_data.pop('password2')
        return User.objects.create_user(**validated_data)


class UserProfileSerializer(serializers.ModelSerializer):
    interests = InterestSerializer(many=True, read_only=True)
    zone_audio_url = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            'id', 'username', 'email', 'bio', 'profile_picture',
            'date_of_birth', 'age_tier', 'interests',
            'latitude', 'longitude',
            'privacy_mode', 'date_joined',
            'zone_audio', 'zone_preset', 'zone_audio_url', 'zone_radius', 'zone_active',
        )
        read_only_fields = ('id', 'email', 'age_tier', 'date_joined')

    def get_zone_audio_url(self, obj):
        if obj.zone_preset and obj.zone_preset in PRESET_MAP:
            return PRESET_MAP[obj.zone_preset]['url']
        if obj.zone_audio:
            return obj.zone_audio.url
        return None


class PublicProfileSerializer(serializers.ModelSerializer):
    interests = InterestSerializer(many=True, read_only=True)
    relationship = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ('id', 'username', 'bio', 'profile_picture', 'interests', 'date_joined', 'relationship')

    def get_relationship(self, obj):
        me = self.context['request'].user
        if FriendRequest.objects.filter(
            Q(sender=me, receiver=obj) | Q(sender=obj, receiver=me),
            status=FriendRequest.Status.ACCEPTED,
        ).exists():
            return 'friend'
        hi = HiRequest.objects.filter(
            Q(sender=me, receiver=obj) | Q(sender=obj, receiver=me)
        ).first()
        if hi:
            if hi.status == HiRequest.Status.ACCEPTED:
                return 'connected'
            return 'hi_sent' if hi.sender_id == me.pk else 'hi_received'
        return 'stranger'


class UserSearchSerializer(serializers.ModelSerializer):
    interests = InterestSerializer(many=True, read_only=True)
    relationship_status = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ('id', 'username', 'profile_picture', 'interests', 'relationship_status')

    def get_relationship_status(self, obj):
        sent = self.context.get('sent_requests', {})
        received = self.context.get('received_requests', {})
        if obj.pk in sent:
            return 'friends' if sent[obj.pk] == 'accepted' else 'pending_sent' if sent[obj.pk] == 'pending' else 'none'
        if obj.pk in received:
            return 'friends' if received[obj.pk] == 'accepted' else 'pending_received' if received[obj.pk] == 'pending' else 'none'
        return 'none'


class ZoneUpdateSerializer(serializers.ModelSerializer):
    zone_audio_url = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ('zone_audio', 'zone_preset', 'zone_audio_url', 'zone_radius', 'zone_active')

    def get_zone_audio_url(self, obj):
        if obj.zone_preset and obj.zone_preset in PRESET_MAP:
            return PRESET_MAP[obj.zone_preset]['url']
        if obj.zone_audio:
            return obj.zone_audio.url
        return None

    def validate_zone_preset(self, value):
        if value and value not in PRESET_MAP:
            raise serializers.ValidationError(f"'{value}' is not a valid preset id.")
        return value

    def validate(self, attrs):
        has_audio = attrs.get('zone_audio') or (
            'zone_audio' not in attrs and self.instance and self.instance.zone_audio
        )
        has_preset = attrs.get('zone_preset') or (
            'zone_preset' not in attrs and self.instance and self.instance.zone_preset
        )
        if attrs.get('zone_audio') and attrs.get('zone_preset'):
            raise serializers.ValidationError('Set either zone_audio or zone_preset, not both.')
        # Uploading a new file clears any existing preset
        if attrs.get('zone_audio') and has_preset:
            attrs['zone_preset'] = ''
        # Selecting a preset clears any existing uploaded file
        if attrs.get('zone_preset') and has_audio:
            attrs['zone_audio'] = None
        return attrs


class EditProfileSerializer(serializers.ModelSerializer):
    interests = serializers.PrimaryKeyRelatedField(
        queryset=Interest.objects.all(),
        many=True,
        required=False,
    )

    class Meta:
        model = User
        fields = ('bio', 'profile_picture', 'date_of_birth', 'interests', 'push_token')

    def update(self, instance, validated_data):
        interests = validated_data.pop('interests', None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()

        if interests is not None:
            instance.interests.set(interests)

        return instance
