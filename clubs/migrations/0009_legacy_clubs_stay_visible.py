"""Every club that existed before location visibility keeps working.

The new column defaults to `city`, which is the right default for a club being
*created* — it fails closed, because a city-scoped club with no city recorded
matches nobody. It is the wrong default for a club that already exists: those
were created with no location at all, there is nothing in the row to infer one
from (clubs have never stored a place), and leaving them at `city` would delete
every existing club from every existing member's discovery in one deploy.

So they are marked `global`, which is the honest description of what they have
always been: visible everywhere, because nobody ever chose otherwise. It is not
selectable at creation, so this set can only shrink — and
`manage.py scope_legacy_clubs` shrinks it, scoping each one to the city its
creator is actually in once we have somewhere to read that from.

Data-only, and it touches nothing but this one column. No club, membership,
message or event is deleted or rewritten.
"""
from django.db import migrations


def mark_existing_as_global(apps, schema_editor):
    Club = apps.get_model('clubs', 'Club')
    # Every row reachable here predates the feature: the columns were added by
    # the migration immediately before this one, so nothing has had a chance to
    # be scoped yet. The `visibility_city__isnull` guard is belt and braces for
    # a re-run.
    Club.objects.filter(visibility_city__isnull=True).update(visibility_level='global')


def restore_default(apps, schema_editor):
    Club = apps.get_model('clubs', 'Club')
    Club.objects.filter(visibility_level='global').update(visibility_level='city')


class Migration(migrations.Migration):

    dependencies = [
        ('clubs', '0008_club_location_visibility'),
    ]

    operations = [
        migrations.RunPython(mark_existing_as_global, restore_default),
    ]
