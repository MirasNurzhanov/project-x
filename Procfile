release: python manage.py migrate && python manage.py seed_interests
web: python manage.py migrate && python manage.py fix_age_tiers && python -c "import core.asgi" && uvicorn core.asgi:application --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="$FORWARDED_ALLOW_IPS" --workers "${WEB_CONCURRENCY:-2}"
worker: python manage.py moderate_swifts --loop
