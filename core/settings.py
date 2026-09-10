import sys
from pathlib import Path
from datetime import timedelta
from decouple import config
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config('SECRET_KEY')

DEBUG = config('DEBUG', default=False, cast=bool)

ALLOWED_HOSTS = ['*']

INSTALLED_APPS = [
    'daphne',
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    'rest_framework',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',
    'channels',
    # Our apps
    'users',
    'interests',
    'proximity',
    'regions',
    'clubs',
    'encounters',
    'notifications',
    'friends',
    'stories',
    'swifts',
    'recs',
    'moderation',
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Above everything that writes a body, so it compresses the finished
    # response. The map poll is ~25KB of JSON every ten seconds per client and
    # the club list ~18KB; both are highly repetitive and compress by roughly
    # 80%. On a phone on mobile data in the Gulf that is the difference the user
    # actually feels, and it costs a few milliseconds of CPU.
    #
    # Safe for this API: the BREACH class of attack needs a secret reflected in
    # a compressed body next to attacker-controlled text, and auth here is a
    # bearer token in a header, which is not compressed.
    "django.middleware.gzip.GZipMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Last: runs after DRF has authenticated the request, so request.user is
    # populated by the time it looks.
    "users.middleware.PresenceMiddleware",
]

ROOT_URLCONF = 'core.urls'

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # Searched *before* the app template directories, which is what lets
        # templates/admin/base_site.html override the one django.contrib.admin
        # ships. See static/admin/css/approx-admin.css for what it loads.
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = 'core.wsgi.application'
ASGI_APPLICATION = 'core.asgi.application'

# ── Database ──────────────────────────────────────────────────────────────────
# The driver is psycopg 3 (the package is `psycopg`, not `psycopg2`). Django
# picks it up from the same `django.db.backends.postgresql` engine name with no
# further configuration — installing it is the whole switch. It is faster than
# psycopg2 on the two things this app does constantly, pipelining statements
# and converting result rows, and unlike psycopg2 it can pool.
#
# ── Pooling ───────────────────────────────────────────────────────────────────
# `CONN_MAX_AGE` keeps one connection per *thread*, which is the wrong unit
# here. Django runs each sync view under asgiref's `ThreadSensitiveContext`,
# whose executor is shut down when the request ends, so the thread — and its
# persistent connection — is discarded with it. Add the eight-worker pool in
# proximity.views and the count stops tracking anything real.
#
# A pool is per *process* and outlives every request in it, which is the unit
# that matches. Connections are handed out and returned rather than opened, so
# the TCP + TLS + authentication round trips disappear from the request path
# instead of merely being amortised over a thread that may not survive.
#
# Django requires CONN_MAX_AGE to be 0 alongside a pool and raises at startup
# otherwise: the two are different answers to the same question, and holding a
# connection open per thread while also pooling would just leak the pool.
DB_POOL = config('DB_POOL', default=True, cast=bool)

# Only consulted when DB_POOL is off. Kept so that turning pooling off is one
# environment variable rather than a revert.
CONN_MAX_AGE = 0 if DB_POOL else config('CONN_MAX_AGE', default=600, cast=int)

# The ceiling is what has to fit the provider's connection limit, and it is now
# a per-process number that stays true regardless of how many threads are in
# flight — which is the point. Budget: processes × DB_POOL_MAX_SIZE, plus
# headroom for a migration and a psql session.
DB_POOL_MIN_SIZE = config('DB_POOL_MIN_SIZE', default=2, cast=int)
DB_POOL_MAX_SIZE = config('DB_POOL_MAX_SIZE', default=12, cast=int)
# How long a request waits for a free connection before failing. Deliberately
# shorter than the statement timeout below: waiting for a slot is queueing, and
# a queue that outlasts the client's patience is worse than a clear error.
DB_POOL_TIMEOUT_S = config('DB_POOL_TIMEOUT_S', default=10.0, cast=float)

# ── Two psycopg 3 levers, both off, both real ─────────────────────────────────
# Left off because neither can be verified without a Postgres to point at, and
# both fail as errors rather than as slowness. Turn them on one at a time in
# staging, exercise the map and the region lookups, and read the logs.
#
# Sending parameters separately from the SQL instead of interpolating them in.
# Cheaper per query, and the precondition for the setting below. It changes how
# Postgres infers parameter types, so a query that leant on the old behaviour
# fails with a type error instead of just running slower.
DB_SERVER_SIDE_BINDING = config('DB_SERVER_SIDE_BINDING', default=False, cast=bool)

# How many times Postgres must see a statement before it prepares and reuses
# the plan. Django pins this to None — no preparation, ever — because prepared
# statements do not survive a transaction-mode connection pooler such as
# PgBouncer, and it cannot know whether one is in front of you.
#
# The pool configured above is psycopg's own and is session-level, so it does
# not have that problem; a managed Postgres with its own pooler in front might.
# Worth trying precisely because the location report replays the same sixteen
# statements from every phone every thirty seconds.
DB_PREPARE_THRESHOLD = config('DB_PREPARE_THRESHOLD', default=0, cast=int)

# A ceiling on any single statement, so one pathological query cannot hold a
# connection indefinitely. It matters more with the pool above than it did
# without: a held connection is now a slot nobody else can have, and
# DB_POOL_TIMEOUT_S turns one stuck query into errors for other requests.
#
# Deliberately not applied to `migrate`: building an index on a large table is
# exactly the kind of long statement this is meant to kill, and killing it
# halfway through a release is worse than any query it protects against.
_DB_STATEMENT_TIMEOUT_MS = config('DB_STATEMENT_TIMEOUT_MS', default=15000, cast=int)
_RUNNING_MIGRATIONS = any(
    arg in sys.argv for arg in ('migrate', 'makemigrations', 'sqlmigrate')
)
_DB_OPTIONS = {}
if _DB_STATEMENT_TIMEOUT_MS > 0 and not _RUNNING_MIGRATIONS:
    _DB_OPTIONS['options'] = f'-c statement_timeout={_DB_STATEMENT_TIMEOUT_MS}'

_database_url = config('DATABASE_URL', default=None)
if _database_url:
    DATABASES = {
        'default': dj_database_url.parse(_database_url, conn_max_age=CONN_MAX_AGE),
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': config('DB_NAME'),
            'USER': config('DB_USER'),
            'PASSWORD': config('DB_PASSWORD'),
            'HOST': config('DB_HOST'),
            'PORT': config('DB_PORT'),
            'CONN_MAX_AGE': CONN_MAX_AGE,
        }
    }

# Applied after the fact so it reaches both branches, and only to PostgreSQL —
# `options` is a libpq connection string and SQLite has no idea what to do with
# it, which is what the test settings run on. The same guard covers the pool:
# it is a psycopg 3 feature and means nothing to any other backend.
if 'postgresql' in DATABASES['default'].get('ENGINE', ''):
    _pg_options = DATABASES['default'].setdefault('OPTIONS', {})
    _pg_options.update(_DB_OPTIONS)
    if DB_SERVER_SIDE_BINDING:
        _pg_options['server_side_binding'] = True
    if DB_PREPARE_THRESHOLD > 0:
        # Only when asked. Django's own default is None, and passing 0 here
        # would mean "prepare everything on first sight", which is not the same
        # request as "leave it alone".
        _pg_options['prepare_threshold'] = DB_PREPARE_THRESHOLD
    if DB_POOL and not _RUNNING_MIGRATIONS:
        # Not during migrations: a migration is one long-lived connection doing
        # one thing, and starting a pool to serve it only adds a way to fail.
        _pg_options['pool'] = {
            'min_size': DB_POOL_MIN_SIZE,
            'max_size': DB_POOL_MAX_SIZE,
            'timeout': DB_POOL_TIMEOUT_S,
        }
    # dj_database_url writes CONN_MAX_AGE into the dict itself, so the value
    # decided above has to be put back over it or Django sees the conflict.
    DATABASES['default']['CONN_MAX_AGE'] = CONN_MAX_AGE

CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
            'hosts': [config('REDIS_URL', default='redis://localhost:6379')],
        },
    },
}

# Shared cache, on the Redis the channel layer already uses.
#
# This is not a performance nicety: regions.nominatim serialises its upstream
# calls through `cache.add` to hold OpenStreetMap's one-request-per-second
# limit, and exceeding that limit gets an application blocked outright. Django's
# default cache is LocMemCache, which is per-process — with more than one worker
# each would keep its own counter and the real rate would be a multiple of the
# limit. Falls back to local memory only when there is no Redis to reach, which
# is the single-process development case where per-process is the whole process.
_REDIS_URL = config('REDIS_URL', default='')
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': _REDIS_URL,
    } if _REDIS_URL else {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    }
}

AUTH_USER_MODEL = 'users.User'

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

REST_FRAMEWORK = {
    # Not simplejwt's own class. This subclass re-reads the suspension columns
    # on every request, which is what makes a ban take effect at once instead
    # of whenever the current access token happens to expire. See
    # users/authentication.py.
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'users.authentication.SuspensionAwareJWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    # orjson in place of the stdlib encoder. Same bytes on the wire — see
    # core/jsonio.py for the compatibility argument and core/test_json.py for
    # the proof. BrowsableAPIRenderer stays second, exactly as in DRF's own
    # default, so opening an endpoint in a browser still works.
    'DEFAULT_RENDERER_CLASSES': (
        'core.jsonio.ORJSONRenderer',
        'rest_framework.renderers.BrowsableAPIRenderer',
    ),
    'DEFAULT_PARSER_CLASSES': (
        'core.jsonio.ORJSONParser',
        'rest_framework.parsers.FormParser',
        'rest_framework.parsers.MultiPartParser',
    ),
    # ── Throttling ────────────────────────────────────────────────────────────
    # `ScopedRateThrottle` and nothing else, deliberately. It is inert on any
    # view that does not declare a `throttle_scope`, so adding it here changes
    # the behaviour of exactly the endpoints that opt in and leaves every
    # existing one alone — which is what makes it safe to introduce to an app
    # that has never had throttling.
    #
    # A blanket `UserRateThrottle` would be the obvious alternative and is the
    # wrong tool: the map poll runs every ten seconds from every client, the
    # unread badge rides on top of it, and a global ceiling tuned to survive
    # those is far too loose to protect anything that actually needs it.
    'DEFAULT_THROTTLE_CLASSES': (
        'rest_framework.throttling.ScopedRateThrottle',
    ),
    'DEFAULT_THROTTLE_RATES': {
        # The one that matters. Without a limit here, anybody who can watch a
        # Swift can walk the id space and harvest every coordinate the app
        # holds — the reveal gate makes each one cost a view, but nothing
        # stopped them from paying that cost a thousand times an hour.
        #
        # Counts refusals as well as grants, which is the point: probing an id
        # that answers 404 is exactly the traffic being limited. Set well above
        # any real session — revealing sixty distinct locations in an hour is
        # not something a person does by watching videos.
        'swift_location_reveal': config('THROTTLE_SWIFT_REVEAL', default='60/hour'),
        # Posting. A ceiling on flooding rather than on people.
        'swift_create': config('THROTTLE_SWIFT_CREATE', default='30/hour'),
        'swift_analytics': config('THROTTLE_SWIFT_ANALYTICS', default='60/min'),
        # Commenting. The only Swift write that both creates a row *and* pushes
        # a notification to somebody else's phone, which makes an unthrottled
        # one a harassment tool rather than a load problem. Set well above
        # normal conversation.
        'swift_comment': config('THROTTLE_SWIFT_COMMENT', default='120/hour'),
        # Reporting. Low, because the only reason to report thirty things an
        # hour is to bury somebody under flags.
        'swift_report': config('THROTTLE_SWIFT_REPORT', default='20/hour'),
        # Every non-Swift report route shares this. Twenty an hour is
        # twenty an hour however they are spread across content types -
        # a client that finds the ceiling on one surface should find the
        # same ceiling on the next.
        'content_report': config('THROTTLE_CONTENT_REPORT', default='20/hour'),
        # Interaction signals. Far higher than the rest on purpose: this is one
        # batch per browsing burst, and every card a viewer scrolls past
        # produces a row. Throttling it the way posting is throttled would
        # silently blind the recommender for anybody who watches a lot, which
        # is precisely the population it most needs to learn from.
        'swift_signals': config('THROTTLE_SWIFT_SIGNALS', default='600/hour'),
    },
}

# ─── Swift recommendations ────────────────────────────────────────────────────
# Every tunable the recommender has, defaulted in `recs/params.py` and
# overridable here. Left empty by default so the shipped values are the ones in
# code and reviewable; staging sets sections to experiment without a deploy.
#
#   SWIFT_RECS = {'weights': {'like': 3.0}, 'diversity': {'exploration_rate': 0.25}}
#
# Unknown keys raise at load rather than being ignored — a typo'd weight that
# silently does nothing is the worst failure a tuning knob can have.
SWIFT_RECS = {}

CSRF_TRUSTED_ORIGINS = config(
    'CSRF_TRUSTED_ORIGINS',
    default='https://*.railway.app,https://*.vercel.app,https://project-x-frontend-alpha.vercel.app',
).split(',')

CORS_URLS_REGEX = r'^.*$'

CORS_ALLOW_ALL_ORIGINS = not DEBUG

CORS_ALLOWED_ORIGINS = config(
    'CORS_ALLOWED_ORIGINS',
    default='http://localhost:3000,http://localhost:5173,http://192.168.68.113:5173,http://192.168.68.113:8000,https://project-x-frontend-alpha.vercel.app',
).split(',')

CORS_ALLOW_CREDENTIALS = True

CORS_ALLOW_HEADERS = [
    'accept',
    'authorization',
    'cache-control',
    'content-type',
    'origin',
    'pragma',
    'x-csrftoken',
    'x-requested-with',
    # Marks a request as coming from a foregrounded app, for the presence
    # heartbeat. See users.middleware.
    'x-aura-presence',
]

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=60),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Cloudflare R2, via django-storages' S3-compatible backend — R2 speaks the S3
# API, so no R2-specific package is needed. `custom_domain` is the public
# bucket URL (R2_PUBLIC_URL) with the scheme stripped, which is the form
# S3Boto3Storage expects: it prefixes whatever it's given with `https://` when
# building a file's `.url`, so passing the scheme through too would double it up.
#
# Hoisted out of the OPTIONS dict below because a second reader appeared:
# `moderation.cloudflare_ai.transform_base` needs the origin these files are
# served from, and `config()` inside a nested literal is not a setting anything
# else can reach. Public by definition - it is the URL every Swift's video is
# already served from - so there is nothing here that was not already going out
# in every feed payload.
R2_PUBLIC_URL = config('R2_PUBLIC_URL')

STORAGES = {
    'default': {
        'BACKEND': 'storages.backends.s3boto3.S3Boto3Storage',
        'OPTIONS': {
            'access_key': config('R2_ACCESS_KEY_ID'),
            'secret_key': config('R2_SECRET_ACCESS_KEY'),
            'bucket_name': config('R2_BUCKET_NAME'),
            'endpoint_url': config('R2_ENDPOINT_URL'),
            'region_name': 'auto',
            'default_acl': 'public-read',
            'custom_domain': R2_PUBLIC_URL.replace('https://', ''),
        },
    },
    'staticfiles': {
        'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
    },
}

# ── AI moderation for Swift video ─────────────────────────────────────────────
# Cloudflare, and only these two variables carry anything secret. They are read
# in exactly one place — `moderation.cloudflare_ai.credentials()` — and nothing
# else in the project is allowed to import them. They are never serialized,
# never logged, never sent to the app, and never written to the database.
#
# `SWIFT_AI_MODERATION_ENABLED` is the master switch and it defaults to **off**,
# which is what makes this change safe to ship before the Cloudflare dashboard
# work is done: with it off, a Swift is published exactly the way it was before
# this feature existed. Turning it on is a variable change, not a release.
#
# The failure mode when it is on and the credentials are missing is deliberate:
# `credentials()` raises, the moderation job fails, and the Swift stays
# `PENDING` — invisible. Nothing gets published unscreened because a variable
# was mistyped. `moderation.checks` warns about that combination at startup so
# it is noticed before a queue builds up.
CLOUDFLARE_API_TOKEN = config('CLOUDFLARE_API_TOKEN', default='')
CLOUDFLARE_ACCOUNT_ID = config('CLOUDFLARE_ACCOUNT_ID', default='')

SWIFT_AI_MODERATION_ENABLED = config(
    'SWIFT_AI_MODERATION_ENABLED', default=False, cast=bool)

# Scout supports image messages and structured output. Older configured models
# retain their image-array transport; changing a model also requires a supported
# input format in moderation.cloudflare_ai.
CLOUDFLARE_AI_VISION_MODEL = config(
    'CLOUDFLARE_AI_VISION_MODEL',
    default='@cf/meta/llama-4-scout-17b-16e-instruct')
CLOUDFLARE_AI_TRANSCRIBE_MODEL = config(
    'CLOUDFLARE_AI_TRANSCRIBE_MODEL', default='@cf/openai/whisper')

# The origin that serves `/cdn-cgi/media/`. Blank means "use R2_PUBLIC_URL",
# which is right whenever the bucket is published on a custom domain in the
# same Cloudflare account. It is a separate setting because Media
# Transformations are enabled **per zone**: a bucket published on an `r2.dev`
# address has no zone to enable them on, and this has to name a custom domain
# instead. See `moderation.cloudflare_ai.transform_base`.
CLOUDFLARE_MEDIA_TRANSFORM_BASE = config(
    'CLOUDFLARE_MEDIA_TRANSFORM_BASE', default='')

# Every Cloudflare call gets this timeout, and there is no call without one. A
# hung inference request holding a worker thread open is how a moderation
# feature takes the whole API down with it.
CLOUDFLARE_AI_TIMEOUT_S = config('CLOUDFLARE_AI_TIMEOUT_S', default=30, cast=int)

# How many frames to look at. Blank/0 means "let the duration decide" — see
# `moderation.policy.frame_count_for`, which spends 5 frames on a short clip
# and 10 on a long one. The cap is the thing that actually protects the bill:
# it is what stops a per-second strategy from ever being possible.
SWIFT_AI_FRAME_COUNT = config('SWIFT_AI_FRAME_COUNT', default=0, cast=int)
SWIFT_AI_FRAME_MAX = config('SWIFT_AI_FRAME_MAX', default=12, cast=int)
#: Frames are scaled down before they are classified. A vision model is billed
#: by the tokens an image becomes, and a 1080-wide frame becomes far more of
#: them than a 640-wide one without saying anything more about whether it
#: contains nudity.
SWIFT_AI_FRAME_WIDTH = config('SWIFT_AI_FRAME_WIDTH', default=640, cast=int)

#: Above this, one frame's objection is recorded as a confident one. Below it,
#: the same objection still quarantines — it is the queue note that changes,
#: not the visibility. See `moderation.policy`.
SWIFT_AI_UNSAFE_CONFIDENCE = config(
    'SWIFT_AI_UNSAFE_CONFIDENCE', default=0.75, cast=float)

#: Attempts before a job stops retrying and the Swift is quarantined for a
#: human instead. Bounded, because "retry until it works" against a paid API
#: that is answering 403 is an unbounded bill.
SWIFT_AI_MAX_ATTEMPTS = config('SWIFT_AI_MAX_ATTEMPTS', default=3, cast=int)
#: Seconds before a job that claims to be running is assumed dead and re-queued
#: by the sweeper. Longer than the worst case for one job (frames x timeout).
SWIFT_AI_STALE_AFTER_S = config('SWIFT_AI_STALE_AFTER_S', default=900, cast=int)

#: Phase 2, off by default. Extracts the audio track through the same Media
#: Transformations path, transcribes it, and runs the transcript through the
#: *existing* text filter — so a spoken slur and a typed one are judged by one
#: set of rules. Off until the transformation origin above is confirmed
#: working, because a second dependency on an unproven path would make a
#: failure harder to read, not the moderation stronger.
SWIFT_AI_AUDIO_MODERATION_ENABLED = config(
    'SWIFT_AI_AUDIO_MODERATION_ENABLED', default=False, cast=bool)

#: Shared secret for the Go moderation worker (services/go-moderation).
#:
#: Blank means the `/api/internal/moderation/` endpoints refuse everything —
#: closed rather than open, which is the opposite of the usual "unset disables
#: the check" default. Getting that backwards would let anything on the network
#: lease moderation jobs and forge verdicts, so the safe direction is the one
#: that stops the worker rather than the one that stops the guard.
MODERATION_WORKER_TOKEN = config('MODERATION_WORKER_TOKEN', default='')

#: Set by `core.test_settings`. Stops the in-process worker thread from being
#: started, so a test drives the pipeline synchronously and nothing outlives
#: the case that spawned it — the same arrangement, and the same reason, as
#: `PUSH_DISABLED` in `notifications.push`.
SWIFT_AI_MODERATION_SYNC = config(
    'SWIFT_AI_MODERATION_SYNC', default=False, cast=bool)

# ── Push dispatcher (the Go service in ../approx-push) ────────────────────────
# Blank means "send pushes the way we always have", which is the default and is
# what every existing deployment keeps doing. Set the URL and this hands
# delivery to `approx-push`, which bounds the fan-out, rate-limits Expo and
# retries transient failures — see notifications/push_dispatcher.py.
#
# In Kubernetes the URL is the Service's cluster DNS name; locally it is
# whatever `go run .` is listening on.
#
#   PUSH_DISPATCHER_URL=http://approx-push.default.svc.cluster.local/v1/push
#   PUSH_DISPATCHER_URL=http://127.0.0.1:8080/v1/push
#
# The token is the same value the Go service reads as PUSH_AUTH_TOKEN. Both
# sides should read it from one secret rather than two copies that can drift.
PUSH_DISPATCHER_URL = config('PUSH_DISPATCHER_URL', default='')
PUSH_DISPATCHER_TOKEN = config('PUSH_DISPATCHER_TOKEN', default='')

# ── Legal ─────────────────────────────────────────────────────────────────────
# Where the Terms of Use, Community Guidelines and Privacy pages live.
#
# This backend serves them itself, at /legal/ (see moderation/legal_views.py),
# because Approx has no marketing domain and a legal document behind a URL that
# does not resolve is the same as not having one. The default is therefore this
# service's own public origin.
#
# Set LEGAL_BASE_URL if a domain is put in front of the API, and set
# LEGAL_PRIVACY_URL on its own if the privacy policy is published somewhere else
# — the app reads whatever `GET /api/users/terms/` hands it, so either is a
# deploy rather than an App Store release.
LEGAL_BASE_URL = config(
    'LEGAL_BASE_URL', default='https://project-x-production-3261.up.railway.app')
LEGAL_PRIVACY_URL = config('LEGAL_PRIVACY_URL', default='')

# ── Terms enforcement window ──────────────────────────────────────────────────
# The login gate (users.views.SuspensionAwareTokenObtainPairSerializer) refuses
# to issue tokens to an account that has not agreed to the current Terms. That
# rule cannot be applied to a build that has never heard of it, and the backend
# has to be live *during* App Review — so it is scoped by client build.
#
#   TERMS_ENFORCEMENT_MIN_BUILD    the first build that gets enforced. Build 32
#                                  is the one that ships the agreement on its
#                                  sign-in screen.
#   TERMS_ENFORCEMENT_ALLOW_LEGACY the compatibility window. True lets clients
#                                  that send no `X-Approx-Build` header through,
#                                  which is build 31 and older.
#
# **Set ALLOW_LEGACY to False once build 31 is off the store.** Until then there
# is an unenforced path, and it stays open only because closing it early would
# lock out every user who has not updated. See `users.terms.terms_enforced_for`.
TERMS_ENFORCEMENT_MIN_BUILD = config('TERMS_ENFORCEMENT_MIN_BUILD', default=32, cast=int)
TERMS_ENFORCEMENT_ALLOW_LEGACY = config(
    'TERMS_ENFORCEMENT_ALLOW_LEGACY', default=True, cast=bool)

#: Published contact address for safety reports and appeals. App Review asks
#: that this be reachable; it is printed on every legal page and linked from
#: Settings.
SAFETY_CONTACT_EMAIL = config(
    'SAFETY_CONTACT_EMAIL', default='nurdauletaskar551@gmail.com')

STATIC_URL = "static/"

# The Approx admin skin lives here rather than inside an app, because it is not
# any one app's asset. Found through the staticfiles finders, which is also how
# core/asgi.py serves it — there is no collectstatic step in this deployment.
STATICFILES_DIRS = [BASE_DIR / "static"]

# Only consulted by `manage.py collectstatic`; the finder path above does not
# need it. Named so that running collectstatic is not an error.
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# ── Region boundaries ─────────────────────────────────────────────────────────
# Where "cover my whole city" gets the city's real outline. The public
# Nominatim instance is free and fine at this size because every lookup is
# cached into a Region row forever after — but its usage policy requires a
# User-Agent that identifies the app and reaches a real person, and it forbids
# bulk use. Point NOMINATIM_URL at a self-hosted or commercial instance if the
# lookup volume ever stops being incidental.
NOMINATIM_URL = config('NOMINATIM_URL', default='https://nominatim.openstreetmap.org')
NOMINATIM_USER_AGENT = config(
    'NOMINATIM_USER_AGENT', default='Approx/1.0 (support@aura-app.com)'
)

# Load shaping. The defaults are the public instance's published policy — one
# request per second, one at a time — and exist as settings so that pointing
# NOMINATIM_URL at an instance you run yourself can come with the limits that
# instance actually has, rather than a code change.
#
# MIN_INTERVAL of 0 disables throttling outright. Never do that against the
# public endpoint: it is the condition of using it.
NOMINATIM_MIN_INTERVAL_S = config('NOMINATIM_MIN_INTERVAL_S', default=1.1, cast=float)
NOMINATIM_MAX_CONCURRENCY = config('NOMINATIM_MAX_CONCURRENCY', default=1, cast=int)
# Queueing for a slot and waiting on the network are budgeted separately, so a
# caller that queued does not arrive at the request with no time left.
NOMINATIM_MAX_QUEUE_WAIT_S = config(
    'NOMINATIM_MAX_QUEUE_WAIT_S', default=5.0, cast=float
)
NOMINATIM_TIMEOUT_S = config('NOMINATIM_TIMEOUT_S', default=8.0, cast=float)
# A border does not move; "nothing is mapped here" is only true until someone
# maps it. Hence the two orders of magnitude between these.
NOMINATIM_CACHE_TTL_S = config(
    'NOMINATIM_CACHE_TTL_S', default=7 * 24 * 60 * 60, cast=int
)
NOMINATIM_MISS_TTL_S = config('NOMINATIM_MISS_TTL_S', default=10 * 60, cast=int)
# Not a cache of failure so much as a few seconds of "we just tried that", so
# one outage does not become a queue of callers rediscovering it one slot at a
# time.
NOMINATIM_ERROR_TTL_S = config('NOMINATIM_ERROR_TTL_S', default=3.0, cast=float)
NOMINATIM_COALESCE_WAIT_S = config(
    'NOMINATIM_COALESCE_WAIT_S', default=13.0, cast=float
)

# ── Boundary relations, direct from OSM ───────────────────────────────────────
# The last stage of the district pipeline, and the only one that does not need
# the place to be *named* in Nominatim's address hierarchy — it asks which
# administrative relations enclose the coordinate. That is what reaches wards in
# Tanzania, phường in Vietnam and 区 in Japan, all of which are mapped and most
# of which the hierarchy does not name.
#
# It runs behind three caches and only on a miss, so its volume against a free
# public service stays proportionate. Set OVERPASS_ENABLED=False to drop the
# stage entirely — the rest of the pipeline is unaffected.
OVERPASS_ENABLED = config('OVERPASS_ENABLED', default=True, cast=bool)
OVERPASS_URL = config(
    'OVERPASS_URL', default='https://overpass-api.de/api/interpreter'
)
# Overpass permits ~2 req/s; this path is rare enough that one is plenty.
OVERPASS_MIN_INTERVAL_S = config('OVERPASS_MIN_INTERVAL_S', default=1.0, cast=float)
OVERPASS_MAX_CONCURRENCY = config('OVERPASS_MAX_CONCURRENCY', default=1, cast=int)
OVERPASS_MAX_QUEUE_WAIT_S = config('OVERPASS_MAX_QUEUE_WAIT_S', default=4.0, cast=float)
# Overpass runs a query rather than a lookup, so it is slower by design.
OVERPASS_TIMEOUT_S = config('OVERPASS_TIMEOUT_S', default=20.0, cast=float)
OVERPASS_QUERY_TIMEOUT_S = config('OVERPASS_QUERY_TIMEOUT_S', default=15, cast=int)
OVERPASS_CACHE_TTL_S = config(
    'OVERPASS_CACHE_TTL_S', default=7 * 24 * 60 * 60, cast=int
)
OVERPASS_MISS_TTL_S = config('OVERPASS_MISS_TTL_S', default=10 * 60, cast=int)
OVERPASS_ERROR_TTL_S = config('OVERPASS_ERROR_TTL_S', default=3.0, cast=float)
OVERPASS_COALESCE_WAIT_S = config(
    'OVERPASS_COALESCE_WAIT_S', default=24.0, cast=float
)

# ── District resolution ───────────────────────────────────────────────────────
# How sure the pipeline has to be before it answers instead of asking. A wrong
# region is not wrong once — it is stored, and every later lookup is served from
# the store — so the bar is deliberately high and near-ties are never broken
# silently.
REGION_MIN_CONFIDENCE = config('REGION_MIN_CONFIDENCE', default=45, cast=int)
REGION_AMBIGUITY_MARGIN = config('REGION_AMBIGUITY_MARGIN', default=15, cast=int)
REGION_MAX_NAME_SEARCHES = config('REGION_MAX_NAME_SEARCHES', default=3, cast=int)
REGION_MAX_CANDIDATES = config('REGION_MAX_CANDIDATES', default=5, cast=int)
# Largest hand-traced boundary accepted, in square degrees. A district is not
# the size of a country, and the point of tracing is a shape too small to be
# mapped rather than one too big.
REGION_MAX_TRACED_AREA_DEG2 = config(
    'REGION_MAX_TRACED_AREA_DEG2', default=1.0, cast=float
)

# Per-country admin_level overrides, merged over regions/admin_levels.py.
# Shaped {'kz': {'city': (4,), 'district': (6, 7)}}. Left empty here because the
# table in that module is the documented default; this exists so a deployment
# can correct one country without a code change.
REGION_ADMIN_LEVELS = {}

# ── Apple Music ───────────────────────────────────────────────────────────────
# Credentials for the catalogue proxy in users/applemusic.py. All three come
# from the Apple Developer portal: a MusicKit key (.p8), the key's id, and the
# team id. Absent, /api/users/zone/apple-music/ answers 503 and the app hides
# the feature — the rest of the zone works untouched.
#
# APPLE_MUSIC_PRIVATE_KEY holds the *contents* of the .p8, not a path: the file
# cannot be committed and there is nowhere to put it on a Railway dyno. Literal
# `\n` in the variable is fine; applemusic.py unescapes it.
APPLE_MUSIC_TEAM_ID = config('APPLE_MUSIC_TEAM_ID', default='')
APPLE_MUSIC_KEY_ID = config('APPLE_MUSIC_KEY_ID', default='')
APPLE_MUSIC_PRIVATE_KEY = config('APPLE_MUSIC_PRIVATE_KEY', default='')
#: Which country's catalogue to search when the client does not say. Availability
#: and preview URLs genuinely differ by storefront.
APPLE_MUSIC_STOREFRONT = config('APPLE_MUSIC_STOREFRONT', default='us')

# ── Epidemic Sound ────────────────────────────────────────────────────────────
# Credentials for the Swift music catalogue importer in swifts/music/epidemic.py.
#
# ┌──────────────────────────────────────────────────────────────────────────┐
# │  THE API KEY GOES HERE — as environment variables, never in this file.    │
# │                                                                          │
# │  Local:    backend/project-x/.env      (git-ignored; see .env.example)    │
# │  Railway:  project → Variables tab                                        │
# └──────────────────────────────────────────────────────────────────────────┘
#
#     EPIDEMIC_SOUND_CLIENT_ID=your-access-key-id
#     EPIDEMIC_SOUND_CLIENT_SECRET=your-access-key-secret
#
# Where they come from, and the part that is easy to get wrong: these are
# issued under Epidemic Sound's **partner/API agreement**, not with an ordinary
# subscription. A personal or commercial subscription licenses *you* to put
# their music in *your* content; Approx is a platform whose users attach music to
# videos Approx then redistributes, which is a different licence entirely. There
# is no self-serve key for it — it comes from talking to them.
#
# Absent, the importer answers "not configured" and names the missing variable,
# `python manage.py sync_music --status` says so, and the rest of Swifts is
# untouched: the picker reads Approx's own `SwiftTrack` table either way, so a
# hand-curated catalogue works with none of this set.
EPIDEMIC_SOUND_CLIENT_ID = config('EPIDEMIC_SOUND_CLIENT_ID', default='')
EPIDEMIC_SOUND_CLIENT_SECRET = config('EPIDEMIC_SOUND_CLIENT_SECRET', default='')

# The licence recorded against every track imported from Epidemic Sound, shown
# in the admin and never sent to creators. A setting rather than a literal
# because the wording that makes a track safe to serve is agreement-specific,
# and it is what somebody will need to read back in two years.
EPIDEMIC_SOUND_LICENCE = config(
    'EPIDEMIC_SOUND_LICENCE', default='Epidemic Sound partner agreement',
)
#: Only if the agreement requires a per-track credit line. Usually blank.
EPIDEMIC_SOUND_ATTRIBUTION = config('EPIDEMIC_SOUND_ATTRIBUTION', default='')

# Endpoints, as settings so that partner documentation can correct them without
# a code change. The defaults are the shape their partner content API uses;
# confirm each against the docs issued with your agreement — they are the one
# part of this integration written without that document in hand.
EPIDEMIC_SOUND_BASE_URL = config(
    'EPIDEMIC_SOUND_BASE_URL',
    default='https://partner-content-api.epidemicsound.com',
)
EPIDEMIC_SOUND_TOKEN_URL = config(
    'EPIDEMIC_SOUND_TOKEN_URL',
    default='https://partner-content-api.epidemicsound.com/v0/token',
)
EPIDEMIC_SOUND_SEARCH_PATH = config(
    'EPIDEMIC_SOUND_SEARCH_PATH', default='/v0/tracks/search',
)
EPIDEMIC_SOUND_TRACK_PATH = config(
    'EPIDEMIC_SOUND_TRACK_PATH', default='/v0/tracks/{id}',
)

# ── Sign in with Google / Sign in with Apple ──────────────────────────────────
# The OAuth client ids whose identity tokens /api/users/social/ will accept, as
# the `aud` claim. Comma-separated, because each platform is a separate client
# and all of them sign in to the same accounts.
#
# Google: list **all three** ids — Web, iOS and Android. The native SDKs are
# configured with the *web* client id in order to be handed an `idToken` at
# all, so that is the audience Google usually stamps; the native ids are here
# because which one appears has moved between SDK versions, and a token minted
# for a client this list does not know answers 400 `audience_mismatch`.
#
# Apple: the app's bundle identifier (com.arcanix.aura). A Services ID belongs
# here too if the web flow is ever added.
#
# Left empty, that provider's half of the endpoint answers 503 `not_configured`
# and the app hides the button rather than offering one that cannot work.
def _csv_setting(name):
    return tuple(p.strip() for p in config(name, default='').split(',') if p.strip())


GOOGLE_OAUTH_CLIENT_IDS = _csv_setting('GOOGLE_OAUTH_CLIENT_IDS')
APPLE_SIGN_IN_CLIENT_IDS = _csv_setting('APPLE_SIGN_IN_CLIENT_IDS')

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
