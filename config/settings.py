"""
Django settings for the Spotter fuel-route API.

Configuration is environment driven so the same image runs locally and in a
container. Two deliberate fallbacks keep a fresh clone runnable with no
infrastructure at all:

    DATABASE_URL unset  -> SQLite file in the project root
    REDIS_URL unset     -> in-process LocMemCache

Both production paths (PostgreSQL, Redis) are exercised by docker-compose.
"""

from pathlib import Path

import dj_database_url
from dotenv import load_dotenv
import os

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------

SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-key-do-not-use-in-production")
DEBUG = env_bool("DEBUG", True)
ALLOWED_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS", "*").split(",") if h.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "apps.stations",
    "apps.routing",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

DATABASES = {
    "default": dj_database_url.config(
        default=os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
        conn_max_age=env_int("DB_CONN_MAX_AGE", 600),
        conn_health_checks=True,
    )
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

REDIS_URL = os.getenv("REDIS_URL", "").strip()

if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
            "KEY_PREFIX": "fuelroute",
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "fuelroute-locmem",
            "OPTIONS": {"MAX_ENTRIES": 2000},
        }
    }

ROUTE_CACHE_TTL_SECONDS = env_int("ROUTE_CACHE_TTL_SECONDS", 60 * 60 * 24)

# --------------------------------------------------------------------------
# REST framework
# --------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
    "UNAUTHENTICATED_USER": None,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Spotter Fuel Route API",
    "DESCRIPTION": (
        "Given a start and finish in the USA, returns the driving route plus the "
        "cost-optimal sequence of diesel stops for a truck with a 500-mile range "
        "at 10 miles per gallon."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# --------------------------------------------------------------------------
# Domain configuration
# --------------------------------------------------------------------------

# Vehicle envelope, straight from the assessment brief.
VEHICLE_MAX_RANGE_MILES = env_float("VEHICLE_MAX_RANGE_MILES", 500.0)
VEHICLE_MPG = env_float("VEHICLE_MPG", 10.0)

# How far off the route a truckstop may sit and still count as usable.
MAX_DETOUR_MILES = env_float("MAX_DETOUR_MILES", 10.0)

# Radius searched around the origin for the departure fill-up.
ORIGIN_FUEL_RADIUS_MILES = env_float("ORIGIN_FUEL_RADIUS_MILES", 30.0)

# Routing provider. The OSRM demo server needs no API key, which keeps a fresh
# clone runnable without credentials.
ROUTING_PROVIDER = os.getenv("ROUTING_PROVIDER", "osrm")
OSRM_BASE_URL = os.getenv("OSRM_BASE_URL", "https://router.project-osrm.org")
VALHALLA_BASE_URL = os.getenv("VALHALLA_BASE_URL", "https://valhalla1.openstreetmap.de")
ROUTING_TIMEOUT_SECONDS = env_float("ROUTING_TIMEOUT_SECONDS", 15.0)

# Consulted only when the primary provider is unreachable, so the healthy path
# stays at one external call. Blank disables the fallback entirely.
ROUTING_FALLBACK_PROVIDER = os.getenv("ROUTING_FALLBACK_PROVIDER", "valhalla")

STATIONS_CSV = BASE_DIR / "data" / "fuel-prices-for-be-assessment.csv"
STATIONS_GEOCODED_CSV = BASE_DIR / "data" / "stations_geocoded.csv"
GEONAMES_CACHE = BASE_DIR / "data" / "us_cities.csv"

# --------------------------------------------------------------------------
# I18N / static
# --------------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "%(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "root": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", "INFO")},
}
