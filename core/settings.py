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

_database_url = config('DATABASE_URL', default=None)
if _database_url:
    DATABASES = {'default': dj_database_url.parse(_database_url, conn_max_age=600)}
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': config('DB_NAME'),
            'USER': config('DB_USER'),
            'PASSWORD': config('DB_PASSWORD'),
            'HOST': config('DB_HOST'),
            'PORT': config('DB_PORT'),
            'CONN_MAX_AGE': 60,
        }
    }

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