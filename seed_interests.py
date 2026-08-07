"""Seed the backend Interest table from interests_seed.json.

PREFER THE MANAGEMENT COMMAND. The backend now ships
`interests/management/commands/seed_interests.py`, and the Procfile's release
step runs it on every deploy:

  release: python manage.py migrate && python manage.py seed_interests

so production is seeded without anyone remembering to do anything. That command
also regroups rows that predate the `category` column, which this script cannot
do. Use it:

  python manage.py seed_interests            # local
  railway run python manage.py seed_interests  # prod
  python manage.py seed_interests --dry-run  # report only

This file remains only as the shell-pipe fallback for a checkout too old to
have that command:

  python manage.py shell < seed_interests.py

A copy of this file and interests_seed.json lives in the backend repo root
(next to manage.py); run it there. The `interests` imports below only resolve
inside the backend project, so an editor open on the mobile repo flags them —
that is expected.

Existing interests are kept (matched by name, case-insensitively); only missing
ones are created, so it is safe to run more than once.
"""
import json
import os

from interests import categories  # type: ignore  # backend (project-x) app
from interests.models import Interest  # type: ignore  # backend (project-x) app

# `__file__` is undefined when piped into `manage.py shell`, so fall back to
# the working directory (the backend root, where the JSON is copied).
try:
    here = os.path.dirname(os.path.abspath(__file__))
except NameError:
    here = os.getcwd()

path = os.path.join(here, 'interests_seed.json')
with open(path, encoding='utf-8') as f:
    catalog = json.load(f)

existing = {name.lower() for name in Interest.objects.values_list('name', flat=True)}
created = 0
unknown_headings = []

for heading, items in catalog.items():
    # The heading is the interest's group, and it has to be carried onto the
    # row. Creating without it leaves `category` blank on all 681, which is
    # what the clients render as a single "More" pile instead of nine groups.
    key = categories.slug_for(heading)
    if not key:
        unknown_headings.append(heading)
    for item in items:
        if item['name'].lower() in existing:
            continue
        Interest.objects.create(name=item['name'], icon=item['icon'], category=key)
        existing.add(item['name'].lower())
        created += 1

if unknown_headings:
    print(
        'Unrecognised headings (their interests have no group): '
        + ', '.join(unknown_headings)
    )

ungrouped = Interest.objects.filter(category='').count()
print(
    f'Done: {created} interests created, {Interest.objects.count()} total '
    f'({ungrouped} ungrouped).'
)
