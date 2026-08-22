from django.urls import path

from apps.routing.views import HealthView

app_name = "routing"

urlpatterns = [
    path("health/", HealthView.as_view(), name="health"),
]
