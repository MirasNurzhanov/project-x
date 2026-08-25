"""Screening, as a serializer mixin.

The point of putting it here rather than writing `validate_caption` five times
is that "which fields count as user-generated text" is then a declaration on
each serializer instead of a habit somebody has to remember. A new field on a
serializer that is not in `screened_fields` is visibly not screened, which is a
much easier thing to catch in review than an absent method.

    class StoryCreateSerializer(ScreenedTextMixin, serializers.ModelSerializer):
        screened_fields = ('content', 'place_name')

    ...
    verdict = serializer.moderation_verdict
    story = serializer.save(moderation=services.moderation_for(verdict))
"""
from rest_framework import serializers

from . import services
from .text import Verdict


class ScreenedTextMixin:
    """Refuses blocked text at validation, and reports the verdict to `save()`.

    Blocked text raises `ValidationError` with a `code`, so a client can tell
    "this broke the guidelines" apart from "this field was too long" without
    string-matching the message. Flagged text validates normally - it is
    publishable in the sense that it may be stored, and it is the caller's job
    to store it quarantined.
    """

    #: Names of the fields on this serializer that carry user-written text.
    screened_fields = ()

    #: Set by `validate`, read by whoever calls `save()`. Defaults to an
    #: allowing verdict so a serializer used without validation - which should
    #: not happen, but does in tests - fails open to "nothing to do" rather
    #: than raising an AttributeError at the save site.
    moderation_verdict = Verdict()

    def validate(self, attrs):
        attrs = super().validate(attrs)
        values = [attrs.get(name) for name in self.screened_fields]
        verdict = services.screen(*values, raise_on_block=False)
        if verdict.blocked:
            raise serializers.ValidationError(
                {
                    'detail': verdict.message(),
                    # Stable, machine-readable, and the thing the app switches
                    # on to show its own copy rather than echoing ours.
                    'code': 'objectionable_content',
                },
                code='objectionable_content',
            )
        self.moderation_verdict = verdict
        return attrs
