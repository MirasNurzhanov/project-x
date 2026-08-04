from django.db import models

from . import categories


class Interest(models.Model):
    name = models.CharField(max_length=50, unique=True)
    icon = models.CharField(max_length=100, blank=True)
    # Which of the nine groups this belongs to — see interests/categories.py.
    # The seed file has always carried this and it had nowhere to go, so the
    # app received 681 interests as one undifferentiated list and each screen
    # was left to guess its own grouping from the names.
    #
    # Blank is a real value: a custom entry from the old "Other" flow belongs to
    # no group, and the clients show those under a trailing "More" heading
    # rather than pretending they are Sports.
    category = models.CharField(
        max_length=20, choices=categories.CHOICES, blank=True, db_index=True,
    )
    # User-typed "Other" entries that didn't match the app's hobby catalog;
    # hidden from other users' interest list until reviewed in the admin.
    needs_review = models.BooleanField(default=False)

    def __str__(self):
        return self.name

    @property
    def category_label(self):
        """The heading this appears under. Empty when it has no group.

        Sent alongside the key so a client that has not heard of a category
        added after it shipped can still print a correct heading, instead of
        dropping those interests into "More".
        """
        return categories.LABELS.get(self.category, '')
