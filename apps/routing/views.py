"""HTTP surface for the routing app."""

from __future__ import annotations

import django
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.shortcuts import render
from django.views import View
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.routing.providers import RouteNotFound, RoutingError, RoutingUnavailable
from apps.routing.resolver import LocationNotFound
from apps.routing.serializers import RouteQuerySerializer, StationSerializer
from apps.routing.services import payload_for_token, plan_journey
from apps.stations.models import Station


class HealthView(APIView):
    """Liveness and readiness probe.

    Reports which database and cache backend the process resolved to,
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
        try:
            return Station.objects.count()
        except Exception:
            return 0


class RouteView(APIView):
    """Plan a journey and cost its fuel."""

    @extend_schema(
        summary="Plan a route and its fuel stops",
        description=(
            "Returns the driving route between two points in the USA together "
            "with the cost-optimal sequence of diesel stops for a truck with a "
            "500 mile range at 10 miles per gallon, and the total spent on fuel.\n\n"
            "One external routing call is made per journey. Repeat journeys are "
            "served from cache and make none."
        ),
        parameters=[
            OpenApiParameter("start", str, description='e.g. "Dallas, TX"'),
            OpenApiParameter("finish", str, description='e.g. "Chicago, IL"'),
            OpenApiParameter("max_detour_miles", float, required=False),
        ],
        responses={200: dict, 400: dict, 404: dict, 503: dict},
    )
    def get(self, request: Request) -> Response:
        return self._plan(request.query_params)

    @extend_schema(
        summary="Plan a route and its fuel stops",
        request=RouteQuerySerializer,
        responses={200: dict, 400: dict, 404: dict, 503: dict},
    )
    def post(self, request: Request) -> Response:
        return self._plan(request.data)

    def _plan(self, data) -> Response:
        form = RouteQuerySerializer(data=data)
        form.is_valid(raise_exception=True)

        try:
            payload = plan_journey(
                form.validated_data["start"],
                form.validated_data["finish"],
                form.validated_data["max_detour_miles"],
            )
        except LocationNotFound as exc:
            return Response(
                {"error": "location_not_found", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except RouteNotFound as exc:
            return Response(
                {"error": "route_not_found", "detail": str(exc)},
                status=status.HTTP_404_NOT_FOUND,
            )
        except RoutingUnavailable as exc:
            return Response(
                {"error": "routing_unavailable", "detail": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except RoutingError as exc:
            return Response(
                {"error": "routing_error", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(payload, status=status.HTTP_200_OK)


class RouteMapView(View):
    """Render a planned journey as an interactive map.

    A plain Django view rather than a DRF one: it serves HTML, and routing it
    through content negotiation only invites a renderer it does not have.
    """

    def get(self, request, token: str):
        payload = payload_for_token(token)
        if payload is None:
            return render(
                request,
                "routing/map_missing.html",
                {"token": token},
                status=404,
            )
        return render(request, "routing/map.html", {"payload": payload})


class StationListView(ListAPIView):
    """Browse the loaded truckstop prices."""

    serializer_class = StationSerializer

    @extend_schema(
        summary="List truckstops",
        parameters=[
            OpenApiParameter("state", str, required=False, description="Two-letter state code"),
            OpenApiParameter("search", str, required=False, description="Match name or city"),
            OpenApiParameter("ordering", str, required=False, description="retail_price or -retail_price"),
        ],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        queryset = Station.objects.all()
        params = self.request.query_params

        state = params.get("state")
        if state:
            queryset = queryset.filter(state__iexact=state.strip())

        search = params.get("search")
        if search:
            queryset = queryset.filter(name__icontains=search) | queryset.filter(
                city__icontains=search
            )

        ordering = params.get("ordering", "retail_price")
        allowed = {"retail_price", "-retail_price", "name", "-name", "city", "-city"}
        return queryset.order_by(ordering if ordering in allowed else "retail_price")
