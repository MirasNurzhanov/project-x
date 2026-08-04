from django.db import migrations


def collapse_everyone_to_friends(apps, schema_editor):
    # "Everyone" and "Friends" now behave identically for zone_audio — see
    # users.visibility.can_view, which caps this field at friend/connected
    # regardless of the stored value. Normalising existing rows keeps the
    # privacy screen's picker (which no longer offers "Everyone" for this
    # field) showing a selection that matches what is actually in effect.
    User = apps.get_model('users', 'User')
    User.objects.filter(visibility_zone_audio='everyone').update(visibility_zone_audio='friends')


class Migration(migrations.Migration):
    dependencies = [
        ('users', '0019_merge_20260804_1510'),
    ]

    operations = [
        migrations.RunPython(collapse_everyone_to_friends, migrations.RunPython.noop),
    ]
