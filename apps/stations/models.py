"""Truckstop storage.

The supplied price file carries no coordinates, so geocoding is a first-class
concern of this model rather than an afterthought: every row records where its
position came from and how confident we are in it.
"""

from django.db import models
from django.db.models import F, Q


class GeocodePrecision(models.TextChoices):
    """Where a station's coordinates came from, best last."""

    UNKNOWN = "unknown", "Not geocoded"
    CITY = "city", "City centroid"
    POI = "poi", "Matched OSM fuel POI"


class StationQuerySet(models.QuerySet):
    def geocoded(self) -> "StationQuerySet":
        return self.filter(latitude__isnull=False, longitude__isnull=False)

    def in_states(self, states) -> "StationQuerySet":
        return self.filter(state__in=[s.upper() for s in states])

    def cheapest_first(self) -> "StationQuerySet":
        return self.order_by("retail_price")


class Station(models.Model):
    """One truckstop, deduplicated to a single expected price.

    The source file repeats 597 stations with differing prices under the same
    OPIS and rack ID and carries no observation date, so those rows are read as
    samples over time. We store their mean as the expected price and keep the
    spread visible via ``price_sample_count``, ``price_min`` and ``price_max``
    rather than silently collapsing it.
    """

    opis_id = models.IntegerField(unique=True, db_index=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=120)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.IntegerField(null=True, blank=True)

    retail_price = models.DecimalField(
        max_digits=8,
        decimal_places=5,
        help_text="Mean of all observed prices for this station, USD per gallon.",
    )
    price_sample_count = models.PositiveSmallIntegerField(default=1)
    price_min = models.DecimalField(max_digits=8, decimal_places=5)
    price_max = models.DecimalField(max_digits=8, decimal_places=5)

    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    geocode_precision = models.CharField(
        max_length=12,
        choices=GeocodePrecision.choices,
        default=GeocodePrecision.UNKNOWN,
        db_index=True,
    )

    objects = StationQuerySet.as_manager()

    class Meta:
        ordering = ["opis_id"]
        indexes = [
            # Corridor matching scans geocoded rows ordered by price.
            models.Index(fields=["latitude", "longitude"], name="station_latlon_idx"),
            models.Index(fields=["retail_price"], name="station_price_idx"),
            models.Index(fields=["state", "city"], name="station_state_city_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(latitude__isnull=True) | Q(latitude__gte=-90, latitude__lte=90),
                name="station_latitude_in_range",
            ),
            models.CheckConstraint(
                condition=Q(longitude__isnull=True) | Q(longitude__gte=-180, longitude__lte=180),
                name="station_longitude_in_range",
            ),
            models.CheckConstraint(
                condition=Q(retail_price__gt=0),
                name="station_price_positive",
            ),
            models.CheckConstraint(
                condition=Q(price_min__lte=F("price_max")),
                name="station_price_spread_ordered",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) ${self.retail_price}/gal"

    @property
    def is_geocoded(self) -> bool:
        return self.latitude is not None and self.longitude is not None
