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
    'cloudinary_storage',
    'cloudinary',
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
        "DIRS": [],
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
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
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
}

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

CLOUDINARY_STORAGE = {
    'CLOUD_NAME': config('CLOUDINARY_CLOUD_NAME'),
    'API_KEY': config('CLOUDINARY_API_KEY'),
    'API_SECRET': config('CLOUDINARY_API_SECRET'),
    'MEDIA_TAG': 'media',
    'INVALID_VIDEO_ERROR_MESSAGE': 'Please upload a valid audio file',
    'EXCLUDE_DELETE_ORPHANED_MEDIA_UNDER_FOLDER': False,
}
STORAGES = {
    "default": {
        "BACKEND": "cloudinary_storage.storage.MediaCloudinaryStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

STATIC_URL = "static/"

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
    'NOMINATIM_USER_AGENT', default='Aura/1.0 (support@aura-app.com)'
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

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"