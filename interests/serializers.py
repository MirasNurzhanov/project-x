from rest_framework import serializers

from . import categories
from .models import Interest


class InterestSerializer(serializers.ModelSerializer):
    is_selected = serializers.SerializerMethodField()
    #: Human heading, sent alongside the key so a client that shipped before a
    #: category existed can still print a correct section title rather than
    #: filing those interests under "More".
    category_label = serializers.CharField(read_only=True)

    class Meta:
        model = Interest
        fields = ('id', 'name', 'icon', 'category', 'category_label', 'is_selected')

    def get_is_selected(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return obj.users.filter(pk=request.user.pk).exists()
        return False


def category_summary(counts):
    """The nine headings in display order, given {key: count}.

    Empty categories are included rather than dropped: an empty section is a
    fact about the catalogue that a picker should show honestly, and dropping
    them would make the layout jump around as the seed file grows.
    """
    summary = [
        {'key': key, 'label': label, 'count': counts.get(key, 0)}
        for key, label in categories.CATEGORIES
    ]
    ungrouped = counts.get('', 0)
    if ungrouped:
        # Only when there is something in it. Unlike the nine, this is not a
        # group anyone chose — it is where custom entries from the retired
        # "Other" flow ended up.
        summary.append({'key': '', 'label': 'More', 'count': ungrouped})
    return summary
