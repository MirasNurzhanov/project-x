from django.db import migrations, models


def pause_everyone(apps, schema_editor):
    """Take every existing account off the map.

    The point of the whole change, applied to the people who are already here.
    Anyone who signed up before this was on the map because `location_sharing`
    defaulted to Live and they had granted a location permission — which is
    precisely what App Review rejected under 5.1.2(i). Changing the default only
    helps accounts created afterwards, so the existing rows are moved too.

    Not reversible in any meaningful sense, and `noop` says so rather than
    guessing: there is no record of who had deliberately chosen Live before
    this, so a reverse would have to invent consent for everybody.
    """
    apps.get_model('users', 'User').objects.update(location_sharing='paused')


class Migration(migrations.Migration):
    """One axis for map visibility, defaulting to off.

    Removes the check-in columns added in 0028. They were a second flag beside
    `location_sharing` that had to agree with it — the three mode buttons are
    the only control now, so the flag was fully determined by the field and the
    pair could only drift. 0028 is left in place rather than rewritten because
    it may already have been applied locally; this runs cleanly either way.
    """

    dependencies = [
        ('users', '0028_map_check_in'),
    ]

    operations = [
        migrations.RemoveField(model_name='user', name='is_checked_in'),
        migrations.RemoveField(model_name='user', name='checked_in_at'),
        migrations.AlterField(
            model_name='user',
            name='location_sharing',
            field=models.CharField(
                choices=[
                    ('live', 'Live'),
                    ('approximate', 'Approximate'),
                    ('paused', 'Paused'),
                ],
                db_index=True,
                default='paused',
                max_length=11,
            ),
        ),
        migrations.RunPython(pause_everyone, migrations.RunPython.noop),
    ]
