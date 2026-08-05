from django.utils.functional import cached_property
from rest_framework import serializers
from users.models import User
from .models import Encounter


class EncounterUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ('id', 'username', 'profile_picture')


class EncounterSerializer(serializers.ModelSerializer):
    other_user = serializers.SerializerMethodField()
    acknowledged = serializers.SerializerMethodField()
    is_mutual = serializers.SerializerMethodField()

    class Meta:
        model = Encounter
        fields = (
            'id', 'other_user', 'distance_km', 'timestamp',
            'acknowledged', 'is_mutual',
        )

    def _me(self):
        return self.context['request'].user.pk

    @cached_property
    def _other_user_serializer(self):
        # Built once per request rather than once per encounter: see the same
        # note in proximity.serializers. Constructing a ModelSerializer is not
        # free — it introspects the model every time.
        return EncounterUserSerializer(context=self.context)

    def get_other_user(self, obj):
        other = obj.user_two if obj.user_one_id == self._me() else obj.user_one
        return self._other_user_serializer.to_representation(other)

    def get_acknowledged(self, obj):
        if obj.user_one_id == self._me():
            return obj.acknowledged_by_one
        return obj.acknowledged_by_two

    def get_is_mutual(self, obj):
        return obj.acknowledged_by_one and obj.acknowledged_by_two
