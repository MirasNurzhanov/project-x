from django.db import migrations, models


def mode_to_type(apps, schema_editor):
    """Carry every existing zone across to the explicit three-way type.

    Three cases, and the third is the one worth being careful about:

      radius                    → circle      (legacy circles, radius intact)
      region + a Region row     → that Region's own kind
      region + no Region row    → circle

    The last is a zone whose cached Region was evicted (`zone_region` is
    SET_NULL). It has no boundary to cover, and `effective_zone_mode` was
    already drawing it as a circle, so calling it one changes nothing that was
    actually happening. Naming it a city here would instead manufacture rows
    that raise ZoneBoundaryUnavailable on every containment check.

    Regions classified `other` become CITY, and that is a grandfathering
    decision rather than a classification one. `other` means *neither* of the
    two sizes the product offers — regions.nominatim names provinces and
    countries as the cases — so a new zone can no longer be created against one
    at all; the serializer refuses it outright.

    Existing rows are kept as they are because they are already being drawn as
    that polygon today, and silently converting them to a circle would shrink a
    zone its owner can see without them touching anything. Nothing consumes
    containment yet, so no such zone is currently a membership surface. These
    are the rows to revisit when the first consumer is wired — they should be
    rare in any case, since the picker only ever searches `city,district`.
    """
    User = apps.get_model('users', 'User')

    User.objects.filter(zone_mode='radius').update(zone_type='circle')
    User.objects.filter(zone_mode='region', zone_region__isnull=True).update(zone_type='circle')
    User.objects.filter(zone_mode='region', zone_region__kind='district').update(zone_type='district')
    User.objects.filter(
        zone_mode='region', zone_region__isnull=False,
    ).exclude(zone_region__kind='district').update(zone_type='city')


def type_to_mode(apps, schema_editor):
    """Reverse: collapse the three types back to two.

    Lossy by nature — city and district both become `region`, and which one a
    user had chosen is not recoverable from `zone_mode` alone. Written anyway so
    the migration can be rolled back without hand-editing rows; re-applying it
    reads the type back off the Region's kind, which is where it came from.
    """
    User = apps.get_model('users', 'User')
    User.objects.filter(zone_type='circle').update(zone_mode='radius')
    User.objects.filter(zone_type__in=('district', 'city')).update(zone_mode='region')


class Migration(migrations.Migration):
    """Replace `zone_mode` (radius|region) with `zone_type` (circle|district|city).

    Added, backfilled, then the old column dropped, in that order and in one
    migration: the backfill reads `zone_mode`, so it cannot run after the drop.
    """

    dependencies = [
        ('users', '0025_remove_user_music_zone_separate'),
        # Not `0001_initial`: the backfill reads `Region.kind`, which arrives in
        # 0002 and is re-backfilled in 0003 after a bad first pass. Depending on
        # the latest keeps this from running against a column whose values are
        # known to have been wrong.
        ('regions', '0006_region_region_kind_lat_idx_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='zone_type',
            field=models.CharField(
                choices=[
                    ('circle', 'Circle around you'),
                    ('district', 'District boundary'),
                    ('city', 'City boundary'),
                ],
                default='circle',
                db_index=True,
                max_length=8,
            ),
        ),
        migrations.RunPython(mode_to_type, type_to_mode),
        migrations.RemoveField(model_name='user', name='zone_mode'),
    ]
