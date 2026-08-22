from django.contrib import admin

from apps.stations.models import Station


@admin.register(Station)
class StationAdmin(admin.ModelAdmin):
    list_display = (
        "opis_id",
        "name",
        "city",
        "state",
        "retail_price",
        "price_sample_count",
        "geocode_precision",
    )
    list_filter = ("state", "geocode_precision")
    search_fields = ("name", "city", "address", "opis_id")
    ordering = ("retail_price",)
