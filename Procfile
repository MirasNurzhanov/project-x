release: python manage.py migrate && python manage.py seed_interests
web: python manage.py migrate && python manage.py fix_age_tiers && daphne -b 0.0.0.0 -p $PORT core.asgi:application
