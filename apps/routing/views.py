"""HTTP surface for the routing app.

P0 ships the health probe only. Route planning, the station browser and the
Leaflet map arrive in P5.
"""

import django
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView


class HealthView(APIView):
    """Liveness and readiness probe.

    Reports which database and cache backend the process actually resolved to,
    so a misconfigured environment is visible without reading the settings.
    """

    @extend_schema(
        summary="Service health",
        description=(
            "Returns process liveness plus the resolved database and cache "
            "backends and the number of loaded truckstops."
        ),
        responses={200: dict, 503: dict},
    )
    def get(self, request: Request) -> Response:
        checks: dict[str, object] = {}
        healthy = True

        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            checks["database"] = {
                "ok": True,
                "engine": connection.settings_dict["ENGINE"].rsplit(".", 1)[-1],
            }
        except Exception as exc:  # pragma: no cover - exercised only when the DB is down
            healthy = False
            checks["database"] = {"ok": False, "error": str(exc)}

        try:
            cache.set("health:ping", "pong", 10)
            hit = cache.get("health:ping") == "pong"
            checks["cache"] = {
                "ok": hit,
                "backend": settings.CACHES["default"]["BACKEND"].rsplit(".", 1)[-1],
            }
            healthy = healthy and hit
        except Exception as exc:  # pragma: no cover
            healthy = False
            checks["cache"] = {"ok": False, "error": str(exc)}

        checks["stations_loaded"] = self._station_count()

        payload = {
            "status": "ok" if healthy else "degraded",
            "django_version": django.get_version(),
            "vehicle": {
                "max_range_miles": settings.VEHICLE_MAX_RANGE_MILES,
                "mpg": settings.VEHICLE_MPG,
                "tank_gallons": settings.VEHICLE_MAX_RANGE_MILES / settings.VEHICLE_MPG,
            },
            "routing_provider": settings.ROUTING_PROVIDER,
            "checks": checks,
        }
        code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
        return Response(payload, status=code)

    @staticmethod
    def _station_count() -> int:
        """Station model lands in P1; report zero until the table exists."""
        try:
            from apps.stations.models import Station

            return Station.objects.count()
        except Exception:
            return 0
