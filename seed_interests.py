"""Seed the backend Interest table from interests_seed.json.

The mobile app fetches interests from GET /api/interests/, so new interests
must be added in the backend database. A copy of this file and
interests_seed.json lives in the backend repo root (next to manage.py) —
run it there:

  python manage.py shell < seed_interests.py

(Against prod: `railway run python manage.py shell < seed_interests.py`.)

Existing interests are kept (matched by name, case-insensitive); only
missing ones are created, so it is safe to run more than once.

This copy is the source of truth alongside the generated seed JSON; the
import below only resolves inside the backend project, so the editor flags
it here — that is expected.
"""
import json
import os

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
for category, items in catalog.items():
    for item in items:
        if item['name'].lower() in existing:
            continue
        Interest.objects.create(name=item['name'], icon=item['icon'])
        existing.add(item['name'].lower())
        created += 1

print(f'Done: {created} interests created, {Interest.objects.count()} total.')
