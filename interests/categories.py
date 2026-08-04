"""The nine groups the interest catalogue is organised into.

`interests_seed.json` has always been written this way — 681 interests under
nine headings — and the grouping was thrown away at seed time, because the
model had nowhere to put it. So the API returned one flat alphabetical list of
681 rows, and each screen was left to invent its own grouping from the names.
The onboarding screen did exactly that, with a regex, and most of the catalogue
fell through it into a bucket called "Casual".

This is the mapping made explicit and given stable keys. The keys are what the
app matches on; the labels are what the seed file uses and what a client that
has never heard of a new category can still print as a heading.

Order is display order, widest interest first. It is deliberately *not*
alphabetical: "Sports" leads because it is the largest group and the one people
open first.
"""

#: (key, label) in display order. The label must match `interests_seed.json`
#: exactly — `slug_for` is what keeps the two in step, and the seeding command
#: fails loudly rather than silently filing an unknown heading under nothing.
CATEGORIES = (
    ('sports', 'Sports'),
    ('fitness', 'Fitness & Wellness'),
    ('music', 'Music'),
    ('arts', 'Arts & Creativity'),
    ('food', 'Food & Drink'),
    ('travel', 'Travel & Outdoors'),
    ('games', 'Games & Tech'),
    ('entertainment', 'Entertainment & Culture'),
    ('lifestyle', 'Lifestyle & Social'),
)

KEYS = tuple(key for key, _ in CATEGORIES)
LABELS = dict(CATEGORIES)
CHOICES = CATEGORIES

#: Position of each key, for ordering rows in SQL. Anything uncategorised sorts
#: after every real category rather than first — an interest with no group is a
#: loose end, and loose ends belong at the bottom of a list, not the top.
ORDER = {key: index for index, key in enumerate(KEYS)}
UNCATEGORISED_ORDER = len(KEYS)

_BY_LABEL = {label.casefold(): key for key, label in CATEGORIES}

#: Interests that exist in the database but not in the seed file, and where
#: they belong.
#:
#: All three come from `interests/migrations/0003_seed_interests.py`, the
#: original 39-entry list, and were dropped when the catalogue was rewritten —
#: `Gym & Weightlifting` because it is two of the current entries welded
#: together, the other two apparently by oversight. They were never deleted,
#: because people had selected them.
#:
#: Grouped here rather than re-added to the seed file, and the distinction
#: matters: this places the rows that already exist, it does not offer them
#: again. Adding `Gym & Weightlifting` back to the catalogue would put a third
#: chip next to `Gym` and `Weightlifting` for the same thing.
LEGACY_GROUPS = {
    'gym & weightlifting': 'fitness',
    'martial arts': 'sports',
    'film photography': 'arts',
}


def slug_for(label):
    """The key for a seed-file heading, or '' if it is not one we know.

    Returning '' rather than raising is deliberate: a heading nobody has
    mapped yet should cost those interests their grouping, not the whole
    deploy — seeding runs on the release line, and an exception there takes the
    deploy with it.
    """
    return _BY_LABEL.get((label or '').strip().casefold(), '')


def legacy_group_for(name):
    """The group for an interest the seed file no longer lists, or ''."""
    return LEGACY_GROUPS.get((name or '').strip().casefold(), '')
