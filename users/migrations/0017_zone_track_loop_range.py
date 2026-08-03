from django.db import migrations, models


class Migration(migrations.Migration):
    """Which slice of the zone track loops.

    Defaults reproduce today's behaviour exactly: start at 0, and a null end
    meaning "play to the end of the file". Nobody's zone changes until they
    open the trim editor.
    """

    dependencies = [
        ('users', '0016_merge_music_zone_and_nationality'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='zone_track_start_ms',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='user',
            name='zone_track_end_ms',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
