from django.db import migrations

MODERATOR_GROUP = 'Moderator'


def create_group(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Group.objects.get_or_create(name=MODERATOR_GROUP)


def drop_group(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Group.objects.filter(name=MODERATOR_GROUP).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('quests', '0009_alter_questproof_image'),
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.RunPython(create_group, drop_group),
    ]
