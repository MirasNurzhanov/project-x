"""What the client may send, and what it may not.

The signal endpoint is the one place a client writes into the recommender, so
it is also the one place a client could try to poison it. Three defences, all
here:

* **the kind is a closed set.** An unknown kind is a 400, not a silently
  ignored row.
* **the viewer is the request user, never a field.** There is no way to send a
  signal as somebody else, because there is no field for it.
* **no coordinates are accepted.** Not optionally, not ignored-if-present:
  `SwiftSignalSerializer` declares no location field, so a client sending one
  gets it dropped by DRF before validation. A `location_reveal` signal says a
  reveal happened and nothing about where anybody was.

Batching is supported because the alternative is a request per swipe. A feed
session produces a signal for every card — mostly skips — and sending them
individually would put more load on the API than the feed itself.
"""
from rest_framework import serializers

from swifts.models import SwiftVideo

from .models import SwiftSignal

#: The most a client may send in one batch. Chosen so a long browsing session
#: still flushes in one or two requests while a malformed client cannot post a
#: million rows in one go.
MAX_BATCH = 50


class SwiftSignalSerializer(serializers.Serializer):
    """One reported interaction.

    Deliberately a plain `Serializer` rather than a `ModelSerializer`. A
    ModelSerializer's field set follows the model, so adding a column later
    would silently open it to client input — which is exactly how a coordinate
    field would end up writable. This one lists what is accepted, and nothing
    else can arrive.
    """

    kind = serializers.ChoiceField(choices=SwiftSignal.Kind.choices)
    swift_id = serializers.IntegerField(required=False, allow_null=True)
    watch_ms = serializers.IntegerField(required=False, min_value=0, max_value=3_600_000)
    completion_pct = serializers.IntegerField(required=False, min_value=0, max_value=100)
    served_by = serializers.CharField(required=False, allow_blank=True, max_length=24)
    position = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=999)

    def validate_kind(self, value):
        # `share` is declared on the model so that adding sharing later needs
        # no migration, but nothing in Aura emits it. Accepting it now would
        # mean accepting a signal whose meaning is not yet defined.
        if value == SwiftSignal.Kind.SHARE:
            raise serializers.ValidationError('Sharing is not available yet.')
        return value


class SignalBatchSerializer(serializers.Serializer):
    signals = serializers.ListField(
        child=SwiftSignalSerializer(), allow_empty=False, max_length=MAX_BATCH,
    )

    def validate(self, attrs):
        """Every signal that names a Swift must name one this viewer may watch.

        Resolved in one query for the whole batch, and through the same
        visibility gate as everything else. A signal about a Swift the viewer
        cannot see is either a bug or an attempt to influence ranking for
        content they were never served, and both should fail the same way.

        Unknown ids are dropped rather than rejected: a Swift deleted between
        being watched and the batch being flushed is ordinary, and failing the
        whole batch for it would lose the other forty-nine.
        """
        from swifts import visibility as swift_visibility

        viewer = self.context['viewer']
        wanted = {
            s['swift_id'] for s in attrs['signals']
            if s.get('swift_id') is not None
        }
        resolved = {}
        if wanted:
            resolved = {
                row.id: row
                for row in SwiftVideo.objects
                .filter(swift_visibility.watchable_q(viewer))
                .filter(id__in=wanted)
                .select_related('author')
            }
        attrs['resolved'] = resolved
        return attrs
