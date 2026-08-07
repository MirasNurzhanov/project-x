from django.db import migrations, models


class Migration(migrations.Migration):
    """An on/off for the sound zone itself.

    `default=True` is the whole safety story: every existing zone keeps
    broadcasting exactly as it did, and the switch only ever does something once
    somebody turns it off.
    """

    dependencies = [
        ('users', '0023_socialaccount'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='music_zone_active',
            field=models.BooleanField(default=True),
        ),
    ]
