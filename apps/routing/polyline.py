"""Encoded polyline codec.

Both routing providers return geometry in Google's polyline format at
precision 6, which is roughly a tenth of the payload of raw GeoJSON for a
transcontinental route. Decoding it ourselves avoids a dependency and keeps
the hot path free of per-point object churn.
"""

from __future__ import annotations


def decode(encoded: str, precision: int = 6) -> list[tuple[float, float]]:
    """Decode an encoded polyline into ``(latitude, longitude)`` pairs.

    Args:
        encoded: The polyline string.
        precision: Decimal places the encoder used. OSRM's ``polyline6`` and
            Valhalla's shape both use 6; Google's classic format uses 5.
    """
    if not encoded:
        return []

    factor = float(10**precision)
    coordinates: list[tuple[float, float]] = []
    index = 0
    length = len(encoded)
    lat = 0
    lon = 0

    while index < length:
        for axis in range(2):
            result = 0
            shift = 0
            while True:
                if index >= length:
                    raise ValueError("Truncated polyline: ran out of characters mid-value")
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            # The low bit flags a negative value; the rest is the magnitude.
            delta = ~(result >> 1) if result & 1 else result >> 1
            if axis == 0:
                lat += delta
            else:
                lon += delta

        coordinates.append((lat / factor, lon / factor))

    return coordinates


def encode(coordinates: list[tuple[float, float]], precision: int = 6) -> str:
    """Encode ``(latitude, longitude)`` pairs into a polyline string."""
    factor = 10**precision
    chunks: list[str] = []
    previous_lat = 0
    previous_lon = 0

    for latitude, longitude in coordinates:
        lat = round(latitude * factor)
        lon = round(longitude * factor)
        chunks.append(_encode_value(lat - previous_lat))
        chunks.append(_encode_value(lon - previous_lon))
        previous_lat = lat
        previous_lon = lon

    return "".join(chunks)


def _encode_value(value: int) -> str:
    value = ~(value << 1) if value < 0 else value << 1
    out: list[str] = []
    while value >= 0x20:
        out.append(chr((0x20 | (value & 0x1F)) + 63))
        value >>= 5
    out.append(chr(value + 63))
    return "".join(out)
