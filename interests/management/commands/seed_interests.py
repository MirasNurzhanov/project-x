"""Load interests_seed.json into the Interest table.

Runs on every deploy from the Procfile. It used to be a loose script piped
into `manage.py shell`, which meant new interests only reached production if
somebody remembered to run it by hand — and an interest that is not in the
database simply does not exist as far as the app is concerned, however
carefully it was added to the JSON.

Idempotent by design: existing rows are matched case-insensitively and left
alone, so re-running creates nothing and a failed deploy can be retried.
Never deletes. An interest that is removed from the JSON stays in the table,
because people have already selected it and dropping the row would silently
strip it from their profiles.
"""
import json
import os

from django.core.management.base import BaseCommand
from django.db import transaction

from interests import categories
from interests.models import Interest


class Command(BaseCommand):
    help = 'Create any interests from interests_seed.json that do not exist yet.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be created without writing anything.',
        )

    def handle(self, *args, **options):
        # settings.BASE_DIR is the project root, which is where the seed file
        # sits next to manage.py.
        from django.conf import settings
        path = os.path.join(settings.BASE_DIR, 'interests_seed.json')

        if not os.path.exists(path):
            # A missing seed file must not break the deploy: the app runs fine
            # on whatever interests are already in the table.
            self.stderr.write(f'No seed file at {path}; nothing to do.')
            return

        with open(path, encoding='utf-8') as f:
            catalog = json.load(f)

        existing = {
            row.name.lower(): row
            for row in Interest.objects.all().only('id', 'name', 'category')
        }
        to_create = []
        to_regroup = []
        unknown_headings = []

        for heading, items in catalog.items():
            key = categories.slug_for(heading)
            if not key:
                # A heading nobody has mapped yet. Reported rather than raised:
                # this runs on the release line, so failing here would take the
                # deploy down over a grouping.
                unknown_headings.append(heading)
            for item in items:
                name = item['name']
                current = existing.get(name.lower())
                if current is None:
                    row = Interest(name=name, icon=item.get('icon', ''), category=key)
                    to_create.append(row)
                    existing[name.lower()] = row
                    continue
                # Already here, but possibly from before this column existed, or
                # under a heading that has since moved. The category is the one
                # field the seed file is authoritative about for an existing
                # row — the name is the key and the icon may have been edited in
                # the admin, so neither is touched.
                if key and current.category != key:
                    current.category = key
                    to_regroup.append(current)

        # Rows the catalogue has forgotten but the database still holds — see
        # categories.LEGACY_GROUPS. Placed, never re-created.
        for lowered, row in existing.items():
            if row.pk is None or row.category:
                continue
            key = categories.legacy_group_for(lowered)
            if key:
                row.category = key
                to_regroup.append(row)

        if unknown_headings:
            self.stderr.write(
                'Unrecognised headings (their interests will have no group): '
                + ', '.join(unknown_headings)
            )

        if options['dry_run']:
            self.stdout.write(
                f'Would create {len(to_create)} interests '
                f'and regroup {len(to_regroup)}.'
            )
            for i in to_create[:20]:
                self.stdout.write(f'  + {i.name} [{i.category or "ungrouped"}]')
            for i in to_regroup[:20]:
                self.stdout.write(f'  ~ {i.name} → {i.category}')
            return

        with transaction.atomic():
            if to_create:
                Interest.objects.bulk_create(to_create, ignore_conflicts=True)
            if to_regroup:
                Interest.objects.bulk_update(to_regroup, ['category'], batch_size=200)

        ungrouped = Interest.objects.filter(category='').count()
        self.stdout.write(
            f'Interests: {len(to_create)} created, {len(to_regroup)} regrouped, '
            f'{Interest.objects.count()} total ({ungrouped} ungrouped).'
        )
