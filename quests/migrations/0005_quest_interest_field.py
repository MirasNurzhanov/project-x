import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('interests', '0003_seed_interests'),
        ('quests', '0004_friend_meetup_quests'),
    ]

    operations = [
        migrations.AddField(
            model_name='quest',
            name='interest',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='quests',
                to='interests.interest',
            ),
        ),
    ]
