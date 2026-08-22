# Adds the real file fields the R2 migration writes to. Additive only —
# video_url/thumbnail_url/media_id are untouched, so any row from the
# Cloudinary-backed pipeline keeps resolving through them.
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('swifts', '0004_swifttrack_provider_swifttrack_provider_track_id_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='swiftvideo',
            name='video',
            field=models.FileField(blank=True, null=True, upload_to='swifts/'),
        ),
        migrations.AddField(
            model_name='swiftvideo',
            name='thumbnail',
            field=models.ImageField(blank=True, null=True, upload_to='swifts/posters/'),
        ),
    ]
