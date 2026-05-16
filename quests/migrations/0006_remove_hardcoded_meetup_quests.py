from django.db import migrations

HARDCODED_TITLES = [
    'Meet a fitness buddy',
    'Coffee date quest',
    'Gaming session',
    'Sports meetup',
    'Creative hangout',
]


def remove_hardcoded_quests(apps, schema_editor):
    Quest = apps.get_model('quests', 'Quest')
    Quest.objects.filter(title__in=HARDCODED_TITLES).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('quests', '0005_quest_interest_field'),
    ]

    operations = [
        migrations.RunPython(remove_hardcoded_quests, migrations.RunPython.noop),
    ]
