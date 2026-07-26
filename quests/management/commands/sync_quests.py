from django.core.management.base import BaseCommand

from interests.models import Interest
from quests.models import Quest
from quests.sync import sync_quests


class Command(BaseCommand):
    help = (
        'Generate photo-proof quests for any interest that does not have them '
        'yet. Safe to re-run; existing quests are never modified.'
    )

    def handle(self, *args, **options):
        daily, weekly = sync_quests(Quest, Interest)
        self.stdout.write(self.style.SUCCESS(
            f'Created {daily} daily and {weekly} weekly quests '
            f'({Quest.objects.exclude(template_key="").count()} total in catalogue).'
        ))
