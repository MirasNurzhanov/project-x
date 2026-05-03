from django.db import models
from django.conf import settings


class Encounter(models.Model):
    # user_one always has the lower pk — enforced at creation time in record_encounter
    user_one = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='encounters_as_one',
    )
    user_two = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='encounters_as_two',
    )
    distance_km = models.DecimalField(max_digits=5, decimal_places=2)
    timestamp = models.DateTimeField(auto_now_add=True)
    acknowledged_by_one = models.BooleanField(default=False)
    acknowledged_by_two = models.BooleanField(default=False)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f'Encounter: {self.user_one} & {self.user_two} @ {self.timestamp}'
