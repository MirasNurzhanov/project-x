from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class Venue(models.Model):
    """Editorial map places, not reservations or claims of business partnership."""

    name = models.CharField(max_length=160)
    slug = models.SlugField(unique=True)
    address = models.CharField(max_length=250)
    category = models.CharField(max_length=60, default='Shopping mall')
    description = models.CharField(max_length=500, blank=True)
    image_url = models.URLField(blank=True, help_text='Use only imagery you have permission to display.')
    latitude = models.DecimalField(max_digits=10, decimal_places=7,
        validators=[MinValueValidator(-90), MaxValueValidator(90)])
    longitude = models.DecimalField(max_digits=10, decimal_places=7,
        validators=[MinValueValidator(-180), MaxValueValidator(180)])
    google_place_id = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ('name', 'id')
        constraints = [
            models.CheckConstraint(condition=models.Q(latitude__gte=-90, latitude__lte=90), name='venue_valid_latitude'),
            models.CheckConstraint(condition=models.Q(longitude__gte=-180, longitude__lte=180), name='venue_valid_longitude'),
        ]

    def __str__(self):
        return self.name
