from datetime import date
from rest_framework import serializers
from django.contrib.auth.password_validation import validate_password
from .models import User
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

    class Meta:
        model = User
        fields = (
            'id', 'username', 'email', 'bio', 'profile_picture',
            'date_of_birth', 'age_tier', 'interests',
            'latitude', 'longitude',
            'is_discoverable', 'ghost_mode', 'date_joined',
            'zone_audio', 'zone_radius', 'zone_active',
        )
        read_only_fields = ('id', 'email', 'age_tier', 'date_joined')


class PublicProfileSerializer(serializers.ModelSerializer):
    interests = InterestSerializer(many=True, read_only=True)

    class Meta:
        model = User
        fields = ('id', 'username', 'bio', 'profile_picture', 'age_tier', 'interests')


class ZoneUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ('zone_audio', 'zone_radius', 'zone_active')


class EditProfileSerializer(serializers.ModelSerializer):
    interests = serializers.PrimaryKeyRelatedField(
        queryset=Interest.objects.all(),
        many=True,
        required=False,
    )

    class Meta:
        model = User
        fields = ('bio', 'profile_picture', 'date_of_birth', 'interests')

    def update(self, instance, validated_data):
        interests = validated_data.pop('interests', None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()

        if interests is not None:
            instance.interests.set(interests)

        return instance
