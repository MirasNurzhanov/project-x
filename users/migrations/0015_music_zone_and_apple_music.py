import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    """A music zone of its own, and an Apple Music track to put in it.

    Every field is additive with a default that reproduces today's behaviour:
    `music_zone_separate=False` means earshot keeps following `zone_radius`, and
    an empty Apple track means the preset/upload pair still decides what plays.
    """

    dependencies = [
        ('users', '0014_user_zone_mode_user_zone_region_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='music_zone_separate',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='user',
            name='music_zone_radius',
            field=models.IntegerField(
                default=200,
                validators=[
                    django.core.validators.MinValueValidator(50),
                    django.core.validators.MaxValueValidator(5000),
                ],
            ),
        ),
        migrations.AddField(
            model_name='user',
            name='music_zone_alerts',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='user',
            name='zone_apple_id',
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name='user',
            name='zone_apple_title',
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name='user',
            name='zone_apple_artist',
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name='user',
            name='zone_apple_artwork',
            field=models.URLField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name='user',
            name='zone_apple_preview_url',
            field=models.URLField(blank=True, max_length=500),
        ),
    ]
