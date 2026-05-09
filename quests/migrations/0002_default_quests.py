from django.db import migrations

DEFAULT_QUESTS = [
    {
        'title': 'First Hi of the Day',
        'description': 'Send a Hi to someone new today.',
        'reward_points': 10,
        'quest_type': 'daily',
        'condition_type': 'send_hi',
    },
    {
        'title': 'Conversation Starter',
        'description': 'Send a message to one of your friends.',
        'reward_points': 15,
        'quest_type': 'daily',
        'condition_type': 'send_message',
    },
    {
        'title': 'Check In',
        'description': 'Update your location to discover who is nearby.',
        'reward_points': 5,
        'quest_type': 'daily',
        'condition_type': 'update_location',
    },
    {
        'title': 'New Connection',
        'description': 'Accept a Hi request and make a new friend this week.',
        'reward_points': 50,
        'quest_type': 'weekly',
        'condition_type': 'make_friend',
    },
    {
        'title': 'Chance Encounter',
        'description': 'Cross paths with another Aura user in the real world.',
        'reward_points': 25,
        'quest_type': 'weekly',
        'condition_type': 'have_encounter',
    },
]


def create_default_quests(apps, schema_editor):
    Quest = apps.get_model('quests', 'Quest')
    Quest.objects.bulk_create([Quest(**q) for q in DEFAULT_QUESTS])


def delete_default_quests(apps, schema_editor):
    Quest = apps.get_model('quests', 'Quest')
    Quest.objects.filter(title__in=[q['title'] for q in DEFAULT_QUESTS]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('quests', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(create_default_quests, delete_default_quests),
    ]
