"""Group the interests that are already in the table.

The column added by the migration before this one is blank on every existing
row, and there are 681 of them in production. They are not unknowable — the
grouping is right there in `interests_seed.json`, which is where every one of
them came from; it simply had nowhere to be stored until now.

So this reads the seed file and files each row under its heading, matching on
name case-insensitively, exactly as the seeding command matches. Rows the seed
file does not mention — custom entries from the retired "Other" flow — keep a
blank category, which is a real answer that the clients render as "More".

Reversible: it only ever writes this one column, and going back blanks it.
Nothing here creates, deletes or renames an interest, so nobody's selections
are touched either way.
"""
import json
import os

from django.db import migrations


def _catalog(apps):
    """The seed file as {lowercased name: category key}, or {} if missing.

    A missing seed file leaves every row uncategorised rather than failing the
    migration: this runs on the release line, and an exception here would take
    the whole deploy with it for the sake of a cosmetic grouping.
    """
    from django.conf import settings
    from interests import categories

    path = os.path.join(settings.BASE_DIR, 'interests_seed.json')
    if not os.path.exists(path):
        return {}

    with open(path, encoding='utf-8') as f:
        catalog = json.load(f)

    mapping = {}
    for label, items in catalog.items():
        key = categories.slug_for(label)
        if not key:
            continue
        for item in items:
            mapping[item['name'].strip().lower()] = key
    # Rows from the original seed migration that the rewritten catalogue no
    # longer lists. Still selected by people, so still worth a heading.
    for name, key in categories.LEGACY_GROUPS.items():
        mapping.setdefault(name, key)
    return mapping


def backfill(apps, schema_editor):
    Interest = apps.get_model('interests', 'Interest')
    mapping = _catalog(apps)
    if not mapping:
        return

    updates = []
    for interest in Interest.objects.all().only('id', 'name', 'category'):
        key = mapping.get(interest.name.strip().lower(), '')
        if key and interest.category != key:
            interest.category = key
            updates.append(interest)

    if updates:
        Interest.objects.bulk_update(updates, ['category'], batch_size=200)


def clear(apps, schema_editor):
    Interest = apps.get_model('interests', 'Interest')
    Interest.objects.update(category='')


class Migration(migrations.Migration):

    dependencies = [
        ('interests', '0005_interest_category'),
    ]

    operations = [
        migrations.RunPython(backfill, clear),
    ]
