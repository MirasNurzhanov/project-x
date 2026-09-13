release: python manage.py migrate && python manage.py seed_interests
web: python manage.py migrate && python manage.py fix_age_tiers && uvicorn core.asgi:application --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="$FORWARDED_ALLOW_IPS"
worker: python manage.py moderate_swifts --loop
