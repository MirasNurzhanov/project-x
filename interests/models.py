from django.db import models


class Interest(models.Model):
    name = models.CharField(max_length=50, unique=True)
    icon = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return self.name
