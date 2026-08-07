from django.db import migrations


class Migration(migrations.Migration):
    """Drop `music_zone_separate`.

    It answered "does the music get its own radius, or does it borrow the map
    zone's", and the answer is now always "its own". A music zone that silently
    followed `zone_radius` was indistinguishable from not having one, and the
    flag being off is what used to take the ring off the map — so the switch
    that means "no music zone" is `music_zone_active`, and this one has nothing
    left to say.

    No data to carry across: everyone keeps their `music_zone_radius`, which for
    anyone who never separated the two is the 200m default rather than whatever
    their zone radius happened to be. That is the point of the change.
    """

    dependencies = [
        ('users', '0024_user_music_zone_active'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='user',
            name='music_zone_separate',
        ),
    ]
