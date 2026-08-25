"""What a report looks like coming in.

`reason` is required and comes from the fixed list. Free text alone would be
unroutable - a moderation queue is triaged by category long before anybody reads
a sentence - and it is the reason code that decides urgency. Same argument, and
deliberately the same field pair, as `swifts.serializers.SwiftReportSerializer`,
so one `ReportSheet` in the app can post to either endpoint unchanged.
"""
from rest_framework import serializers

from .reasons import Reason


class ReportSerializer(serializers.Serializer):
    reason = serializers.ChoiceField(choices=Reason.choices)
    note = serializers.CharField(
        max_length=500, allow_blank=True, required=False, default='',
    )

    def validate_note(self, value):
        return (value or '').strip()
