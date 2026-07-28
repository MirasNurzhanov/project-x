from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from notifications.utils import send_push_notification

User = get_user_model()


class Command(BaseCommand):
    help = 'Send a test push notification to a user by email'

    def add_arguments(self, parser):
        parser.add_argument('email', type=str, help='Email of the target user')

    def handle(self, *args, **options):
        email = options['email']
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            raise CommandError(f'No user found with email: {email}')

        if not user.push_token:
            raise CommandError(f'User {email} has no push_token set')

        send_push_notification(user, 'Test 🎉', 'Push notification working!')
        self.stdout.write(self.style.SUCCESS(f'Push sent to {email}'))
