import os

from django.apps import AppConfig


class RoutingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.routing"
    label = "routing"

    def ready(self) -> None:
        """Optionally warm the indexes at start-up.

        Off by default: ``ready`` also runs for migrations, shell sessions and
        the test suite, none of which should be touching the station table.
        Containers set WARM_INDEXES_ON_START and pay the cost once, up front.
        """
        if os.getenv("WARM_INDEXES_ON_START", "").strip().lower() not in {"1", "true", "yes"}:
            return

        from django.db import connection

        try:
            if not connection.introspection.table_names():
                return
            from apps.routing.corridor import get_station_index
            from apps.routing.resolver import get_place_index

            get_station_index()
            get_place_index()
        except Exception:  # pragma: no cover - start-up must never be fatal here
            import logging

            logging.getLogger(__name__).warning("Index warm-up skipped", exc_info=True)
