"""Undo 0002's backfill, which guessed wrong.

0002 filled `kind` from admin_level: 6-8 city, 9+ district. That mapping does
not hold. Almaty's city *districts* are admin_level 6, so every one of them was
labelled a city — and because the for-point lookup answers from cache before it
ever asks upstream, a mislabelled row silently became the permanent answer to
"what city am I in".

Regions are a pure cache of public geometry, so the honest repair is to forget
what we think we know and let each row re-derive its size from a fresh payload.
Setting 'other' rather than deleting keeps the rows — and the zones pointing at
them — intact: 'other' matches neither lookup, so the next request for that
place goes upstream, and nominatim._region_from_payload corrects the row in
place from the response.

The cost is one upstream call per place, once. The table is a cache measured in
tens of rows.
"""
from django.db import migrations


def reset_kinds(apps, schema_editor):
    Region = apps.get_model('regions', 'Region')
    Region.objects.exclude(kind='other').update(kind='other')


class Migration(migrations.Migration):

    dependencies = [
        ('regions', '0002_region_kind'),
    ]

    operations = [
        # Irreversible in spirit: there is nothing to restore, because what it
        # removes was wrong. Reversing is a no-op rather than an error so the
        # migration never blocks a rollback of something else.
        migrations.RunPython(reset_kinds, migrations.RunPython.noop),
    ]
