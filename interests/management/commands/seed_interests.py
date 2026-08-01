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

        existing = {n.lower() for n in Interest.objects.values_list('name', flat=True)}
        to_create = []
        for items in catalog.values():
            for item in items:
                name = item['name']
                if name.lower() in existing:
                    continue
                to_create.append(Interest(name=name, icon=item.get('icon', '')))
                existing.add(name.lower())

        if options['dry_run']:
            self.stdout.write(f'Would create {len(to_create)} interests.')
            for i in to_create[:20]:
                self.stdout.write(f'  + {i.name}')
            return

        if to_create:
            with transaction.atomic():
                Interest.objects.bulk_create(to_create, ignore_conflicts=True)

        self.stdout.write(
            f'Interests: {len(to_create)} created, {Interest.objects.count()} total.'
        )
