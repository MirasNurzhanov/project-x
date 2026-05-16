from django.db import migrations


def update_rewards(apps, schema_editor):
    Quest = apps.get_model('quests', 'Quest')
    # Daily quests — target range 10-25 pts
    Quest.objects.filter(condition_type='update_location').update(reward_points=10)
    # Weekly quests — target range 50-75 pts
    Quest.objects.filter(condition_type='have_encounter').update(reward_points=75)
    # Friend/duo quests — 150 pts each
    Quest.objects.filter(condition_type='meet_friend_in_person').update(reward_points=150)


def revert_rewards(apps, schema_editor):
    Quest = apps.get_model('quests', 'Quest')
    Quest.objects.filter(condition_type='update_location').update(reward_points=5)
    Quest.objects.filter(condition_type='have_encounter').update(reward_points=25)
    Quest.objects.filter(condition_type='meet_friend_in_person').update(reward_points=100)


class Migration(migrations.Migration):
    dependencies = [
        ('quests', '0006_remove_hardcoded_meetup_quests'),
    ]

    operations = [
        migrations.RunPython(update_rewards, revert_rewards),
    ]
