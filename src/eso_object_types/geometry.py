from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Sequence

import astropy.units as u
from astropy.coordinates import SkyCoord
from astropy_healpix import HEALPix

from .models import Observation, SearchTarget

_HPX10 = HEALPix(nside=1024, order="nested", frame="icrs")


def validate_position(ra_deg: float, dec_deg: float) -> None:
    if not math.isfinite(ra_deg) or not 0.0 <= ra_deg < 360.0:
        raise ValueError(f"invalid right ascension: {ra_deg!r}")
    if not math.isfinite(dec_deg) or not -90.0 <= dec_deg <= 90.0:
        raise ValueError(f"invalid declination: {dec_deg!r}")


def search_radius_deg(s_fov_deg: float | None, min_radius_arcsec: float) -> float:
    if not math.isfinite(min_radius_arcsec) or min_radius_arcsec <= 0:
        raise ValueError("minimum radius must be positive and finite")
    minimum_deg = min_radius_arcsec / 3600.0
    if s_fov_deg is None:
        return minimum_deg
    try:
        value = float(s_fov_deg)
    except (TypeError, ValueError):
        return minimum_deg
    if not math.isfinite(value) or value <= 0:
        return minimum_deg
    return max(value / 2.0, minimum_deg)


def healpix_order10(ra_deg: float, dec_deg: float) -> int:
    validate_position(ra_deg, dec_deg)
    coordinate = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame="icrs")
    return int(_HPX10.skycoord_to_healpix(coordinate))


def _position_key(ra_deg: float, dec_deg: float, radius_deg: float) -> str:
    return f"{ra_deg:.12f}|{dec_deg:.12f}|{radius_deg:.12f}"


def consolidate_search_targets(
    observations: Iterable[Observation],
) -> list[SearchTarget]:
    grouped: dict[str, list[str]] = defaultdict(list)
    positions: dict[str, tuple[float, float, float]] = {}
    for observation in observations:
        key = _position_key(
            observation.ra_deg,
            observation.dec_deg,
            observation.search_radius_deg,
        )
        grouped[key].append(observation.eso_dp_id)
        positions[key] = (
            observation.ra_deg,
            observation.dec_deg,
            observation.search_radius_deg,
        )

    targets = []
    for number, key in enumerate(sorted(grouped), start=1):
        ra_deg, dec_deg, radius_deg = positions[key]
        targets.append(
            SearchTarget(
                search_key=f"q{number:08d}",
                ra_deg=ra_deg,
                dec_deg=dec_deg,
                radius_deg=radius_deg,
                observation_ids=tuple(sorted(grouped[key])),
            )
        )
    return targets


def chunked(values: Sequence[SearchTarget], size: int) -> Iterable[list[SearchTarget]]:
    if size < 1:
        raise ValueError("chunk size must be at least 1")
    for start in range(0, len(values), size):
        yield list(values[start : start + size])


def batch_hash(service: str, targets: Sequence[SearchTarget]) -> str:
    payload = {
        "service": service,
        "targets": [
            {
                "key": target.search_key,
                "ra": round(target.ra_deg, 12),
                "dec": round(target.dec_deg, 12),
                "radius": round(target.radius_deg, 12),
                "observations": target.observation_ids,
            }
            for target in targets
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()

