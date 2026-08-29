"""Split the old three-way `privacy_mode` into two independent axes and add
the per-field privacy controls.

Effect on existing accounts (deliberate, see the notes on each step):
  * active     -> live         no change
  * ghost      -> approximate  slightly MORE open: previously listed with no
                               coordinates at all, now shown at a point fuzzed
                               to a ~500m grid. Their stored coordinates are
                               re-snapped here so the precise position they had
                               on file is destroyed, not just hidden.
  * invisible  -> paused       no change
  * stories, stats/Approx points and connection lists become Friends-only for
    everyone. Nobody's account becomes more open on these fields.
  * interests stay Everyone (they drive matching), leaderboard stays opt-out,
    search discoverability stays on.
"""
from django.db import migrations, models

from users.visibility import snap_coordinates

MODE_MAP = {
    'active': 'live',
    'ghost': 'approximate',
    'invisible': 'paused',
}


def forwards(apps, schema_editor):
    User = apps.get_model('users', 'User')
    for old, new in MODE_MAP.items():
        User.objects.filter(location_sharing=old).update(location_sharing=new)

    # Ex-ghost accounts opted out of sharing a precise position. Now that the
    # mode puts them back on the map, overwrite what is stored rather than
    # relying on a serializer to keep hiding it.
    approximate = User.objects.filter(
        location_sharing='approximate',
        latitude__isnull=False,
        longitude__isnull=False,
    )
    for user in approximate:
        user.latitude, user.longitude = snap_coordinates(user, user.latitude, user.longitude)
        user.save(update_fields=['latitude', 'longitude'])


def backwards(apps, schema_editor):
    User = apps.get_model('users', 'User')
    for old, new in MODE_MAP.items():
        User.objects.filter(location_sharing=new).update(location_sharing=old)
    # Snapped coordinates are not recoverable; they stay snapped.


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0009_widen_lat_lon_decimal_places'),
    ]

    operations = [
        migrations.RenameField(
            model_name='user',
            old_name='privacy_mode',
            new_name='location_sharing',
        ),
        migrations.AlterField(
            model_name='user',
            name='location_sharing',
            field=models.CharField(
                choices=[('live', 'Live'), ('approximate', 'Approximate'), ('paused', 'Paused')],
                default='live',
                max_length=11,
            ),
        ),
        migrations.RunPython(forwards, backwards),
        migrations.AddField(
            model_name='user',
            name='is_private',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='user',
            name='visibility_interests',
            field=models.CharField(
                choices=[('everyone', 'Everyone'), ('friends', 'Friends'), ('nobody', 'Nobody')],
                default='everyone', max_length=8),
        ),
        migrations.AddField(
            model_name='user',
            name='visibility_stories',
            field=models.CharField(
                choices=[('everyone', 'Everyone'), ('friends', 'Friends'), ('nobody', 'Nobody')],
                default='friends', max_length=8),
        ),
        migrations.AddField(
            model_name='user',
            name='visibility_stats',
            field=models.CharField(
                choices=[('everyone', 'Everyone'), ('friends', 'Friends'), ('nobody', 'Nobody')],
                default='friends', max_length=8),
        ),
        migrations.AddField(
            model_name='user',
            name='visibility_zone_audio',
            field=models.CharField(
                choices=[('everyone', 'Everyone'), ('friends', 'Friends'), ('nobody', 'Nobody')],
                default='friends', max_length=8),
        ),
        migrations.AddField(
            model_name='user',
            name='visibility_connections',
            field=models.CharField(
                choices=[('everyone', 'Everyone'), ('friends', 'Friends'), ('nobody', 'Nobody')],
                default='friends', max_length=8),
        ),
        migrations.AddField(
            model_name='user',
            name='show_on_leaderboard',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='user',
            name='discoverable_by_username',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='user',
            name='discoverable_by_interest',
            field=models.BooleanField(default=True),
        ),
    ]
