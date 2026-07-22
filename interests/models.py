from django.db import models


class Interest(models.Model):
    name = models.CharField(max_length=50, unique=True)
    icon = models.CharField(max_length=100, blank=True)
    # User-typed "Other" entries that didn't match the app's hobby catalog;
    # hidden from other users' interest list until reviewed in the admin.
    needs_review = models.BooleanField(default=False)

    def __str__(self):
        return self.name
