from django.contrib.auth.models import AbstractUser
from django.db import models
from datetime import date


class User(AbstractUser):
    class AgeTier(models.TextChoices):
        MINOR = 'minor', '13-17'
        ADULT = 'adult', '18+'

    email = models.EmailField(unique=True)
    date_of_birth = models.DateField(null=True, blank=True)
    age_tier = models.CharField(
        max_length=5,
        choices=AgeTier.choices,
        blank=True,
    )
    bio = models.TextField(blank=True)
    profile_picture = models.ImageField(upload_to='profile_pictures/', null=True, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    interests = models.ManyToManyField('interests.Interest', blank=True, related_name='users')

    class ZoneRadius(models.IntegerChoices):
        SMALL = 100, '100m'
        MEDIUM = 500, '500m'
        LARGE = 1000, '1000m'

    zone_audio = models.FileField(upload_to='zones/', null=True, blank=True)
    zone_radius = models.IntegerField(choices=ZoneRadius.choices, default=ZoneRadius.MEDIUM)
    zone_active = models.BooleanField(default=False)

    is_discoverable = models.BooleanField(default=True)
    ghost_mode = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')
        if update_fields is None or 'date_of_birth' in update_fields:
            if self.date_of_birth:
                today = date.today()
                age = today.year - self.date_of_birth.year - (
                    (today.month, today.day) < (self.date_of_birth.month, self.date_of_birth.day)
                )
                self.age_tier = self.AgeTier.MINOR if age < 18 else self.AgeTier.ADULT
                if update_fields is not None:
                    kwargs['update_fields'] = list(update_fields) + ['age_tier']
        super().save(*args, **kwargs)

    def __str__(self):
        return self.email
