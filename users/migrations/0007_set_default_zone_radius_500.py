from django.db import migrations


def set_zone_radius_500(apps, schema_editor):
    User = apps.get_model('users', 'User')
    User.objects.exclude(zone_radius=500).update(zone_radius=500)


class Migration(migrations.Migration):
    dependencies = [
        ('users', '0006_privacy_mode'),
    ]

    operations = [
        migrations.RunPython(set_zone_radius_500, migrations.RunPython.noop),
    ]
