"""Run periodically from a scheduler, never from a web request or process boot."""
from django.core.management.base import BaseCommand
from django.utils import timezone

from users.deletion import finalize_deletion
from users.models import User


class Command(BaseCommand):
    help = 'Finalize due account deletions, preserving shared history and moderation evidence.'

    def add_arguments(self, parser):
        parser.add_argument('--execute', action='store_true', help='Perform deletions; default is a dry run.')
        parser.add_argument('--limit', type=int, default=100)

    def handle(self, *args, **options):
        from django.core.management.base import CommandError
        if not 1 <= options['limit'] <= 1000:
            raise CommandError('--limit must be between 1 and 1000.')
        now = timezone.now()
        ids = list(User.objects.filter(
            is_active=False, deletion_requested_at__isnull=False,
            delete_after__lte=now, retired_account_id__isnull=True,
        ).order_by('delete_after', 'pk').values_list('pk', flat=True)[:options['limit']])
        if not options['execute']:
            self.stdout.write(f'Dry run: {len(ids)} account(s) due in this batch.')
            return
        completed = sum(finalize_deletion(pk, now=now) for pk in ids)
        self.stdout.write(f'Finalized {completed} account deletion(s).')
