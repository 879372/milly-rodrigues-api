"""
Django settings for core project.
"""

import sys
from pathlib import Path
import os
import dj_database_url
from dotenv import load_dotenv
from datetime import timedelta
import json

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')


def _env(name, default=''):
    """os.getenv com remoção de aspas externas acidentais (ex.: valor colado como \"False\")."""
    v = os.getenv(name)
    if v is None:
        return default
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        v = v[1:-1].strip()
    return v


DEBUG = _env('DEBUG', 'False').lower() == 'true'


def _env_list(name, default=None):
    """Lê uma variável de ambiente como lista de strings.

    Aceita, de forma tolerante:
      - JSON array:            ["https://a.com", "https://b.com"]
      - JSON array entre aspas: "[\"https://a.com\", \"https://b.com\"]"
      - lista separada por vírgula: https://a.com, https://b.com
      - valor único:           https://a.com
    """
    raw = os.getenv(name)
    if raw is None:
        return list(default or [])
    raw = raw.strip()
    if not raw:
        return list(default or [])

    # remove aspas externas acidentais quando o conteúdo não é um array JSON
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        inner = raw[1:-1].strip()
        if not inner.startswith('['):
            raw = inner

    for _ in range(2):
        try:
            val = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            break
        if isinstance(val, list):
            return [str(x).strip() for x in val if str(x).strip()]
        if isinstance(val, str):
            raw = val.strip()  # era uma string JSON contendo a lista real
            continue
        break

    parts = []
    for p in raw.replace('\n', ',').split(','):
        p = p.strip().strip('"').strip("'").strip()
        if p:
            parts.append(p)
    return parts

# Comandos de build/manutenção que não servem tráfego e não dependem de uma
# SECRET_KEY real (ex.: collectstatic roda na fase de build, antes das envs de runtime).
_BUILD_COMMANDS = {
    'collectstatic', 'makemigrations', 'showmigrations',
    'compilemessages', 'makemessages', 'createcachetable', 'test', 'diffsettings',
    'spectacular',
}
_IS_BUILD_STEP = len(sys.argv) > 1 and sys.argv[1] in _BUILD_COMMANDS

# --- SECRET_KEY: sem default embutido; rejeita valores placeholder conhecidos ---
_INSECURE_SECRETS = {
    'django-insecure-jj^qla6c8*i-j7@w2=jp(jd+n*ejmzljaa4m#!7kiyl0qf97iq',
    'super_secret_key_change_this_in_production',
    'changeme',
    '',
}
SECRET_KEY = _env('SECRET_KEY', '')
_INSECURE_PREFIXES = ('django-insecure-', 'dev-only-', 'dev-local-')
_is_insecure = (
    not SECRET_KEY
    or SECRET_KEY in _INSECURE_SECRETS
    or SECRET_KEY.startswith(_INSECURE_PREFIXES)
)
if _is_insecure:
    if DEBUG or _IS_BUILD_STEP:
        # Chave efêmera: só desenvolvimento local ou passo de build (collectstatic etc.).
        # NUNCA alcança um processo que serve requisições — o gunicorn/runserver
        # não está em _BUILD_COMMANDS, então lá a exceção abaixo é levantada.
        SECRET_KEY = 'dev-only-' + os.urandom(24).hex()
    else:
        raise ImproperlyConfigured(
            "SECRET_KEY ausente ou insegura em produção. Defina a variável de ambiente "
            "SECRET_KEY com um valor aleatório: "
            "python -c \"import secrets; print(secrets.token_urlsafe(64))\""
        )

# ALLOWED_HOSTS vem do ambiente; '*' só é tolerado em DEBUG.
_hosts = _env_list('ALLOWED_HOSTS')
if _hosts:
    ALLOWED_HOSTS = _hosts
elif DEBUG:
    ALLOWED_HOSTS = ['*']
else:
    # Em produção, ALLOWED_HOSTS deve ser informado explicitamente no ambiente.
    ALLOWED_HOSTS = []

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    
    # 3rd party
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',
    'django_filters',
    'drf_spectacular',
    'simple_history',
    
    # Local apps
    'api',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'simple_history.middleware.HistoryRequestMiddleware',
]

ROOT_URLCONF = 'core.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'core.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# Commented out for local testing without Postgres
db_url = os.getenv('DATABASE_URL')
if db_url:
    DATABASES['default'] = dj_database_url.parse(db_url)

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

LANGUAGE_CODE = 'pt-br'
TIME_ZONE = 'America/Sao_Paulo'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}
WHITENOISE_USE_FINDERS = True

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

AUTH_USER_MODEL = 'api.User'

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_FILTER_BACKENDS': ['django_filters.rest_framework.DjangoFilterBackend'],
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
        'rest_framework.throttling.ScopedRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '60/min',
        'user': '2000/min',
        'login': '10/min',
        'phone_lookup': '12/min',
        'portal': '20/min',
        'public_write': '15/min',
    },
}

# Origens do frontend (browser). Usadas tanto para CORS quanto para CSRF.
_frontend_origins = _env_list('ALLOWED_ORIGINS')

# CORS: em dev libera tudo; em produção usa apenas a lista explícita.
# Se ALLOWED_ORIGINS estiver ausente/inválida em produção -> nenhuma origem cross-site (fail closed).
CORS_ALLOW_ALL_ORIGINS = DEBUG
CORS_ALLOWED_ORIGINS = [] if DEBUG else [o for o in _frontend_origins if '://' in o]

CSRF_TRUSTED_ORIGINS = sorted(set(
    [
        "http://localhost:5173",
        "http://localhost:3000",
    ]
    + [o for o in _frontend_origins if '://' in o]
    + [o for o in _env_list('CSRF_TRUSTED_ORIGINS') if '://' in o]
))

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(hours=12),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=30),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': True,
}

# --- Hardening extra em produção ---
if not DEBUG:
    SECURE_SSL_REDIRECT = _env('SECURE_SSL_REDIRECT', 'True').lower() == 'true'
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'

SPECTACULAR_SETTINGS = {
    'TITLE': 'Milly Rodrigues API',
    'DESCRIPTION': 'API de agendamento e gestão para depilação e estética',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
}
