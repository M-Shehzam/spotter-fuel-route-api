"""Request validation for the routing endpoints."""

from django.conf import settings
from rest_framework import serializers


class RouteQuerySerializer(serializers.Serializer):
    """A journey to plan.

    ``start`` and ``finish`` accept a city and state ("Dallas, TX"), a bare
    city name, or a latitude and longitude ("32.7767,-96.7970").
    """

    start = serializers.CharField(
        max_length=200,
        help_text='Where the journey begins, e.g. "Dallas, TX" or "32.7767,-96.7970".',
    )
    finish = serializers.CharField(
        max_length=200,
        help_text='Where the journey ends, e.g. "Chicago, IL".',
    )
    max_detour_miles = serializers.FloatField(
        required=False,
        min_value=0.0,
        max_value=50.0,
        help_text=(
            "How far off the route a truckstop may sit and still be used. "
            "Defaults to the configured corridor width."
        ),
    )

    def validate(self, attrs):
        attrs.setdefault("max_detour_miles", settings.MAX_DETOUR_MILES)
        if attrs["start"].strip().lower() == attrs["finish"].strip().lower():
            raise serializers.ValidationError(
                {"finish": "The finish must differ from the start."}
            )
        return attrs


class StationSerializer(serializers.Serializer):
    """A truckstop from the supplied price file."""

    opis_id = serializers.IntegerField()
    name = serializers.CharField()
    address = serializers.CharField()
    city = serializers.CharField()
    state = serializers.CharField()
    retail_price = serializers.DecimalField(max_digits=8, decimal_places=5)
    price_sample_count = serializers.IntegerField()
    price_min = serializers.DecimalField(max_digits=8, decimal_places=5)
    price_max = serializers.DecimalField(max_digits=8, decimal_places=5)
    latitude = serializers.FloatField(allow_null=True)
    longitude = serializers.FloatField(allow_null=True)
    geocode_precision = serializers.CharField()
