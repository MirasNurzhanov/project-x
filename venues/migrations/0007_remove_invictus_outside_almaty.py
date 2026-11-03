from django.db import migrations

# Only the Almaty Invictus clubs are wanted (2026-11-03). A meetup that used
# one of these keeps its own copy of the place; ClubEvent.venue is SET_NULL.
INVICTUS_OUTSIDE_ALMATY = (
    'invictus-fitness-akbulak',
    'invictus-fitness-green-mall',
    'invictus-fitness-highvill',
    'invictus-fitness-sulo',
    'invictus-fitness-semey',
    'invictus-fitness-al-farabi',
    'invictus-fitness-nursat',
    'invictus-fitness-temirtau',
    'invictus-fitness-oral',
    'invictus-girls-orynbor',
    'invictus-girls-qonayev',
    'invictus-girls-sfera',
    'invictus-girls-tole-bi',
    'invictus-girls-karagandy',
)


def remove(apps, schema_editor):
    Venue = apps.get_model('venues', 'Venue')
    Venue.objects.using(schema_editor.connection.alias).filter(
        slug__in=INVICTUS_OUTSIDE_ALMATY).delete()


class Migration(migrations.Migration):
    dependencies = [('venues', '0006_seed_gym_chains')]
    operations = [migrations.RunPython(remove, migrations.RunPython.noop)]
