# Django, for local development under docker-compose.
#
# This did not exist before the Go services arrived — the app deploys to
# Railway from the Procfile, with no container involved. It is here so
# `docker-compose up` gives you Postgres, Redis, Django and the Go workers
# together, which is the only way to exercise the integration end to end.
#
# It is NOT the production image. Railway still builds from the Procfile, and
# the difference is deliberate: making this the deployment artefact would mean
# maintaining a container build nobody currently needs.
#
# Note how much larger it is than either Go service's, and why: the interpreter
# and every wheel in requirements.txt have to be present at runtime, because
# Python resolves imports when it runs rather than when it builds.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# psycopg[binary] ships a prebuilt libpq so no compiler is needed; Pillow needs
# the image libraries at runtime. This is the class of dependency a Go binary
# simply does not have.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg62-turbo zlib1g \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements first, so the (slow) install layer caches independently of the
# source. Same instinct as `COPY go.mod` in the Go images.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["uvicorn", "core.asgi:application", "--host", "0.0.0.0", "--port", "8000"]
