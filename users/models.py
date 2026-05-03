from django.contrib.auth.models import AbstractUser
from django.db import models


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

    is_discoverable = models.BooleanField(default=True)
    ghost_mode = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    def __str__(self):
        return self.email
