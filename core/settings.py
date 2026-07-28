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
    'quests',
    'market',
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
    # Quest proof photos use authenticated delivery: no public URL, access
    # only via server-signed URLs handed out by the moderation API.
    "quest_proofs": {
        "BACKEND": "quests.storage.QuestProofCloudinaryStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

STATIC_URL = "static/"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# ── Device attestation for quest photo proofs ────────────────────────────────
# Modes: 'off' (ignore), 'audit' (verify + record, never block), 'enforce'
# (a passing attestation is required to submit a proof). Ships as 'audit' so
# the backend can deploy before the store-side setup exists — clients that
# send nothing simply record 'missing'. Flip to 'enforce' once builds with
# @expo/app-integrity are live and the keys below are populated.
QUEST_ATTESTATION_MODE = config('QUEST_ATTESTATION_MODE', default='audit')

# Android — Play Integrity. Keys come from Play Console under
# "App integrity → Response encryption" when you choose to manage your own
# response encryption keys.
ANDROID_PACKAGE_NAME = config('ANDROID_PACKAGE_NAME', default='com.arcanix.aura')
PLAY_INTEGRITY_DECRYPTION_KEY = config('PLAY_INTEGRITY_DECRYPTION_KEY', default='')
PLAY_INTEGRITY_VERIFICATION_KEY = config('PLAY_INTEGRITY_VERIFICATION_KEY', default='')
PLAY_INTEGRITY_PRODUCTION = config('PLAY_INTEGRITY_PRODUCTION', default=True, cast=bool)
PLAY_INTEGRITY_ALLOW_NON_PLAY = config('PLAY_INTEGRITY_ALLOW_NON_PLAY', default=False, cast=bool)
PLAY_INTEGRITY_REQUIRED_VERDICT = config(
    'PLAY_INTEGRITY_REQUIRED_VERDICT', default='MEETS_DEVICE_INTEGRITY'
)

# iOS — App Attest. APPLE_APP_ID is "<10-char Team ID>.<bundle id>".
APPLE_APP_ID = config('APPLE_APP_ID', default='')
# False uses Apple's development attestation environment; set True for
# TestFlight/App Store builds.
APPLE_ATTEST_PRODUCTION = config('APPLE_ATTEST_PRODUCTION', default=False, cast=bool)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"