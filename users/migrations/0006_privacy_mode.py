from django.db import migrations, models


def forward_migrate_privacy(apps, schema_editor):
    User = apps.get_model('users', 'User')
    for user in User.objects.all():
        if user.ghost_mode:
            user.privacy_mode = 'ghost'
        elif not user.is_discoverable:
            user.privacy_mode = 'invisible'
        else:
            user.privacy_mode = 'active'
        user.save(update_fields=['privacy_mode'])


class Migration(migrations.Migration):
    dependencies = [
        ('users', '0005_add_zone_preset'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='privacy_mode',
            field=models.CharField(
                choices=[('active', 'Active'), ('ghost', 'Ghost'), ('invisible', 'Invisible')],
                default='active',
                max_length=9,
            ),
        ),
        migrations.RunPython(forward_migrate_privacy, migrations.RunPython.noop),
        migrations.RemoveField(model_name='user', name='is_discoverable'),
        migrations.RemoveField(model_name='user', name='ghost_mode'),
    ]
