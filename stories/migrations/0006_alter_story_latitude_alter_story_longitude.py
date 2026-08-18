"""Story coordinates become optional.

A story posted while location sharing is Paused carries no position at all, so
the columns have to be able to say so. Nothing is backfilled: every existing
row was posted with a coordinate and keeps it. Whether that coordinate is ever
*shown* is decided on read, in stories/visibility.py, which is what lets an
author pause and retract their pins without losing the stories themselves.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('stories', '0005_story_story_live_box_idx_story_story_author_idx'),
    ]

    operations = [
        migrations.AlterField(
            model_name='story',
            name='latitude',
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),
        migrations.AlterField(
            model_name='story',
            name='longitude',
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),
    ]
