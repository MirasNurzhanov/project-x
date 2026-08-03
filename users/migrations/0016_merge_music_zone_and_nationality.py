from django.db import migrations


class Migration(migrations.Migration):
    """Rejoins the two 0015s.

    The music-zone/Apple-Music migration and the nationality/profession one were
    written on separate branches, both starting from 0014, so the graph had two
    leaves and `migrate` refused to guess an order. This is what
    `makemigrations --merge` produces: no operations, just a single node that
    depends on both, giving the graph one leaf again.

    A merge rather than a renumber on purpose. 0015_music_zone_and_apple_music
    shipped in the "apple music api deploy" commit and is already applied in
    production; renaming it would present it to Django as a new migration and
    it would try to add columns that exist.

    The two touch different columns — music_zone_*/zone_apple_* on one side,
    nationality/profession on the other — so there is nothing to reconcile
    beyond the ordering itself.
    """

    dependencies = [
        ("users", "0015_add_nationality_profession"),
        ("users", "0015_music_zone_and_apple_music"),
    ]

    operations = []
