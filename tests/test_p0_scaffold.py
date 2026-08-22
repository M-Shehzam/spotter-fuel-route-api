"""P0 gate: the scaffold boots, the settings resolve, and health reports truthfully."""

import django
import pytest
from django.conf import settings
from django.urls import reverse


def test_django_is_latest_stable():
    """The brief asks for the latest stable Django. Pin the expectation."""
    major, minor = django.VERSION[:2]
    assert (major, minor) == (6, 1), f"expected Django 6.1, got {django.get_version()}"


def test_domain_settings_match_the_brief():
    assert settings.VEHICLE_MAX_RANGE_MILES == 500.0
    assert settings.VEHICLE_MPG == 10.0
    # 500 miles at 10 mpg is a 50 gallon tank.
    assert settings.VEHICLE_MAX_RANGE_MILES / settings.VEHICLE_MPG == 50.0


def test_routing_provider_needs_no_api_key():
    """A fresh clone must run without credentials."""
    assert settings.ROUTING_PROVIDER == "osrm"
    assert settings.OSRM_BASE_URL.startswith("https://")
    assert "key" not in settings.OSRM_BASE_URL.lower()


def test_source_csv_is_committed():
    assert settings.STATIONS_CSV.exists(), "the supplied price file must ship with the repo"


def test_apps_are_installed():
    assert "apps.stations" in settings.INSTALLED_APPS
    assert "apps.routing" in settings.INSTALLED_APPS
    assert "rest_framework" in settings.INSTALLED_APPS


@pytest.mark.django_db
def test_health_endpoint_reports_ok(client):
    response = client.get(reverse("routing:health"))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["django_version"] == django.get_version()
    assert body["checks"]["database"]["ok"] is True
    assert body["checks"]["cache"]["ok"] is True
    assert body["vehicle"]["tank_gallons"] == 50.0
    # P1 has not loaded anything yet; the probe must not pretend otherwise.
    assert body["checks"]["stations_loaded"] == 0


def test_openapi_schema_is_served(client):
    response = client.get("/api/schema/")
    assert response.status_code == 200


def test_swagger_ui_is_served(client):
    response = client.get("/api/docs/")
    assert response.status_code == 200
