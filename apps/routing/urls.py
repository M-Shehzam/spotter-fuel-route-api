from django.urls import path

from apps.routing.views import HealthView, RouteMapView, RouteView, StationListView

app_name = "routing"

urlpatterns = [
    path("health/", HealthView.as_view(), name="health"),
    path("route/", RouteView.as_view(), name="route"),
    path("route/map/<str:token>/", RouteMapView.as_view(), name="route-map"),
    path("stations/", StationListView.as_view(), name="stations"),
]
