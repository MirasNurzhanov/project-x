from django.db import migrations


def delete_all_clubs(apps, schema_editor):
    Club = apps.get_model("clubs", "Club")
    Club.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("clubs", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(delete_all_clubs, migrations.RunPython.noop),
    ]
