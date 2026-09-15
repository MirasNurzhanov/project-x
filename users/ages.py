from django.utils import timezone


def age_in_years(birthday, today=None):
    if birthday is None:
        return None
    today = today or timezone.localdate()
    if birthday > today:
        return None
    return today.year - birthday.year - ((today.month, today.day) < (birthday.month, birthday.day))
