"""Capture settings a Swift carries: speed, rotation, colour effects, stickers.

Everything here describes **how the recording is played**, never how it was
written. That is the same decision `swifts/media.py` argues for trim and
volume, and it is why these can exist at all: nothing on the phone re-encodes
video and nothing on the server can either, so a "2× speed" Swift is an
ordinary recording plus the number 200, applied by the player.

The one place that gets genuinely subtle is the interaction between speed and
the sixty-second ceiling, which is what `played_ms`/`recorded_ms` below are
for. Read the note on `played_ms`.
"""

#: The slowest and fastest a Swift may play, as a percentage of real time.
#:
#: The range is the client's picker (0.3×, 0.5×, 1×, 2×, 3×) with nothing to
#: spare. Wider would not be a feature: below a third of speed a clip stops
#: reading as slow motion and starts reading as a stall, and above triple the
#: frames a phone actually recorded are too few to carry the motion.
MIN_SPEED_PERCENT = 30
MAX_SPEED_PERCENT = 300

#: The speeds the picker offers. The server accepts anything in range rather
#: than only these — a client is free to offer a finer dial — but this is what
#: the app ships and what the tests pin.
SPEED_CHOICES = (30, 50, 100, 200, 300)

#: How many stickers one Swift may carry.
#:
#: A limit rather than none, because `SwiftVideo.stickers` is an unbounded JSON
#: column on a row every feed page reads: without a ceiling a single post could
#: carry a megabyte of emoji and every viewer would download it. Thirty is far
#: more than anyone arranges by hand.
MAX_STICKERS = 30

#: The longest a single sticker's `emoji` value may be.
#:
#: Generous on purpose — one "emoji" can be a ZWJ sequence of five codepoints
#: plus skin-tone and variation selectors, which is comfortably over twenty
#: UTF-16 units. This is a sanity bound against a text payload smuggled into
#: the field, not a grapheme count.
MAX_STICKER_CHARS = 32

#: The colour treatments the app ships, keyed as the client stores them.
#:
#: The server holds the *names* and nothing else: what each one looks like is a
#: tint, an opacity and a blend mode in `constants/swiftEffects.ts` on the
#: phone, and deliberately so — a filter is a rendering decision and changing
#: how "warm" looks should not be a migration.
#:
#: What this list is for is refusing an `effect` no player knows. A Swift
#: stored with a typo'd key would play unfiltered for everyone, silently, and
#: the creator would never find out.
EFFECT_KEYS = (
    'mono',
    'noir',
    'warm',
    'cool',
    'vivid',
    'fade',
    'sepia',
    'dream',
    'night',
)


def played_ms(recorded_ms, speed_percent):
    """How long a clip runs on screen, given how long it was recorded for.

    This is the direction the sixty-second ceiling cares about. A Swift's
    `duration_ms` is what a viewer experiences, so that is what gets compared
    to `MAX_DURATION_MS` — and it is why a 90-second recording at 150% is a
    perfectly legal 60-second Swift while a 45-second one at 50% is a 90-second
    Swift and is not.

    Rounded rather than truncated: the alternative loses up to a millisecond
    per conversion, and `recorded_ms(played_ms(x))` is a round trip the trim
    maths performs more than once.
    """
    speed = _speed(speed_percent)
    return int(round(recorded_ms * 100 / speed))


def recorded_ms(played, speed_percent):
    """The inverse of `played_ms`: source milliseconds for a given screen time.

    What the camera needs in order to stop itself at the right moment — at 50%
    the recorder may only run for 30 seconds, because those 30 seconds are what
    becomes a 60-second Swift.
    """
    speed = _speed(speed_percent)
    return int(round(played * speed / 100))


def _speed(speed_percent):
    """A usable speed, whatever arrived.

    Clamped rather than raising. This is called from serializer validation and
    from the feed, and a stored row that somehow fell outside the range — an
    old fixture, a hand edit in the admin — should play at the nearest sane
    speed rather than 500ing a feed page for everybody who scrolls past it.
    The serializer is where a *submitted* value out of range is refused.
    """
    try:
        speed = int(speed_percent)
    except (TypeError, ValueError):
        return 100
    if speed < MIN_SPEED_PERCENT:
        return MIN_SPEED_PERCENT
    if speed > MAX_SPEED_PERCENT:
        return MAX_SPEED_PERCENT
    return speed
