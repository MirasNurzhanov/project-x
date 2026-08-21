"""Import a music provider's catalogue into `SwiftTrack`.

    python manage.py sync_music epidemic --term "lo-fi" --limit 50
    python manage.py sync_music epidemic --dry-run
    python manage.py sync_music --status

Nothing imported here is published. Rows arrive with `is_active=False` and a
person turns them on in the admin after reading the licence — see
`swifts/music/sync.py` for why that gate is not something a sync may skip.
"""
from django.core.management.base import BaseCommand, CommandError

from swifts.music import registry, sync
from swifts.music.base import MusicProviderError


class Command(BaseCommand):
    help = 'Import tracks from a music provider into the Swift catalogue.'

    def add_arguments(self, parser):
        parser.add_argument(
            'provider', nargs='?', help='Provider key, e.g. "epidemic".')
        parser.add_argument(
            '--term', default='',
            help='Search term. Blank imports the default listing.')
        parser.add_argument('--limit', type=int, default=50)
        parser.add_argument('--offset', type=int, default=0)
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change without writing.')
        parser.add_argument(
            '--status', action='store_true',
            help='Show which providers are configured, and what is missing.')

    def handle(self, *args, **options):
        if options['status'] or not options['provider']:
            self._print_status()
            return

        key = options['provider']
        try:
            provider = registry.get(key)
        except KeyError as exc:
            raise CommandError(str(exc)) from exc

        missing = provider.missing_settings()
        if missing:
            raise CommandError(
                f'{provider.label} is not configured. '
                f'Set: {", ".join(missing)}.\n'
                'See core/settings.py for where each one comes from.'
            )

        try:
            result = sync.sync_provider(
                key,
                term=options['term'],
                limit=options['limit'],
                offset=options['offset'],
                dry_run=options['dry_run'],
            )
        except MusicProviderError as exc:
            raise CommandError(f'[{exc.code}] {exc}') from exc

        prefix = 'Would import' if options['dry_run'] else 'Imported'
        self.stdout.write(self.style.SUCCESS(
            f'{prefix} from {provider.label}: {result}'))
        if result.created and not options['dry_run']:
            self.stdout.write(
                'New tracks are inactive. Publish them in the admin once you '
                'have checked the licence: /admin/swifts/swifttrack/'
            )

    def _print_status(self):
        for row in registry.status():
            label = row['label']
            if row['configured']:
                self.stdout.write(self.style.SUCCESS(f'  {label}: configured'))
            else:
                missing = ', '.join(row['missing']) or 'credentials'
                self.stdout.write(self.style.WARNING(
                    f'  {label}: not configured - missing {missing}'))
