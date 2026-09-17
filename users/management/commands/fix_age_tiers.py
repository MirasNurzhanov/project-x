from datetime import date

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

User = get_user_model()


class Command(BaseCommand):
    help = 'Recalculate age_tier for every user based on their date_of_birth.'

    def handle(self, *args, **options):
        updated = 0
        skipped = 0
        for user in User.objects.all():
            if not user.date_of_birth:
                skipped += 1
                continue
            today = date.today()
            age = today.year - user.date_of_birth.year - (
                (today.month, today.day) < (user.date_of_birth.month, user.date_of_birth.day)
            )
            correct_tier = User.AgeTier.MINOR if age < 18 else User.AgeTier.ADULT
            if user.age_tier != correct_tier:
                user.age_tier = correct_tier
                user.save(update_fields=['age_tier'])
            updated += 1
        self.stdout.write(self.style.SUCCESS(
            f'Done — {updated} users processed, {skipped} skipped (no date_of_birth).'
        ))
