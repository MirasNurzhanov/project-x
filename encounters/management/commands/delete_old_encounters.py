from django.core.management.base import BaseCommand
from encounters.utils import delete_old_encounters, ENCOUNTER_MAX_AGE_DAYS


class Command(BaseCommand):
    help = f'Delete Encounter records older than {ENCOUNTER_MAX_AGE_DAYS} days.'

    def handle(self, *args, **options):
        delete_old_encounters()
        self.stdout.write(self.style.SUCCESS(
            f'Old encounters (>{ENCOUNTER_MAX_AGE_DAYS}d) deleted.'
        ))
