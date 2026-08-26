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
from rest_framework.exceptions import APIException

from . import services
from .text import Verdict


class ObjectionableContent(APIException):
    """400, with a body a client can actually branch on.

    Deliberately **not** `serializers.ValidationError`. DRF funnels any
    ValidationError raised inside `validate()` through `as_serializer_error`,
    which forces every value into a list - so `{'code': 'objectionable_content'}`
    ships as `{'code': ['objectionable_content']}` and the app has to know to
    index it. A plain `APIException` skips that machinery and the dict arrives
    the shape it was written in.

    The message is deliberately vague about *which* rule fired. A precise error
    is a free oracle for tuning around the filter, and somebody posting this
    material does not need help finding the wording that gets through.
    """

    status_code = 400
    default_code = 'objectionable_content'

    def __init__(self, verdict):
        super().__init__({
            'detail': verdict.message(),
            'code': 'objectionable_content',
        })


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
            raise ObjectionableContent(verdict)
        self.moderation_verdict = verdict
        return attrs
