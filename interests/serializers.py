from rest_framework import serializers
from .models import Interest


class InterestSerializer(serializers.ModelSerializer):
    is_selected = serializers.SerializerMethodField()

    class Meta:
        model = Interest
        fields = ('id', 'name', 'icon', 'is_selected')

    def get_is_selected(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return obj.users.filter(pk=request.user.pk).exists()
        return False
