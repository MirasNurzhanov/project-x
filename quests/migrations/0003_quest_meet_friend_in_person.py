from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('quests', '0002_default_quests'),
    ]

    operations = [
        migrations.AlterField(
            model_name='quest',
            name='condition_type',
            field=models.CharField(
                choices=[
                    ('send_hi', 'Send Hi'),
                    ('send_message', 'Send Message'),
                    ('update_location', 'Update Location'),
                    ('make_friend', 'Make Friend'),
                    ('select_interests', 'Select Interests'),
                    ('have_encounter', 'Have Encounter'),
                    ('meet_friend_in_person', 'Meet Friend in Person'),
                ],
                max_length=25,
            ),
        ),
    ]
