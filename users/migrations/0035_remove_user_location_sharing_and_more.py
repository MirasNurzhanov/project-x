"""Live/Approximate/Paused becomes one Ghost Mode boolean.

Three things collapse here, and only the first is a rename:

* **Paused → `ghost_mode`.** Same idea, opposite polarity, and now mutual: a
  ghost is off everybody's map *and* has nobody on theirs.
* **Live vs Approximate is gone entirely.** Precision stopped being the
  sharer's setting and became the viewer's relationship — friends see the
  precise point, everybody else a ~500m cell, decided per read in
  `users.visibility.coordinates_for`. There is nothing left for a stored mode
  to say.
* **`sharing_session` is gone.** It existed to stop a visibility outliving the
  run of the app that chose it, which is the behaviour being deliberately
  removed: sharing now persists across launches, the way Find My and Snap Map
  do.

**Everybody lands on `ghost_mode=False`, i.e. not hidden**, and that is the
consequential line in this file. It is not a guess about what people wanted:
every account currently reads `paused` regardless of preference, because the
old client ended the session on each cold launch, so there is no signal in the
old column to preserve. Mapping `paused → ghost` would therefore hide every
user in the product on the strength of an artefact.

What keeps that from publishing anyone unannounced is on the client: it reports
no position at all until the user has seen a one-time screen saying they are on
the map, that friends see them precisely, that others see an area, and where
Ghost Mode is. No position, no pin — so the affirmative moment still comes
before anyone is visible, it just happens once instead of every launch.

Not reversible in the sense that matters. The column can be re-added, but the
three-way distinction it held cannot be recovered from a boolean.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0034_user_zone_follow_stale_user_zone_follows_location"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="ghost_mode",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.RemoveField(
            model_name="user",
            name="location_sharing",
        ),
        migrations.RemoveField(
            model_name="user",
            name="sharing_session",
        ),
    ]
