from django.db import migrations, models


class Migration(migrations.Migration):
    """Adds the zone_music choice. Choices are not a database constraint here,
    so this only keeps the schema state and the model in step."""

    dependencies = [
        ("notifications", "0002_notification_story_alter_notification_type"),
    ]

    operations = [
        migrations.AlterField(
            model_name="notification",
            name="notification_type",
            field=models.CharField(
                choices=[
                    ("nearby_user", "Nearby User"),
                    ("story_reaction", "Story Reaction"),
                    ("story_reply", "Story Reply"),
                    ("zone_music", "Zone Music"),
                ],
                max_length=20,
            ),
        ),
    ]
