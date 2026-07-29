import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0001_initial"),
        ("stories", "0002_savedstory"),
    ]

    operations = [
        migrations.AddField(
            model_name="notification",
            name="story",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="notifications",
                to="stories.story",
            ),
        ),
        migrations.AlterField(
            model_name="notification",
            name="notification_type",
            field=models.CharField(
                choices=[
                    ("nearby_user", "Nearby User"),
                    ("story_reaction", "Story Reaction"),
                    ("story_reply", "Story Reply"),
                ],
                max_length=20,
            ),
        ),
    ]
