from __future__ import annotations

import pytest

from eso_object_types.geometry import (
    batch_hash,
    consolidate_search_targets,
    healpix_order10,
    search_radius_deg,
    validate_position,
)
from eso_object_types.models import Observation


def observation(identifier: str, ra: float = 10.0) -> Observation:
    return Observation(
        eso_dp_id=identifier,
        obs_publisher_did=None,
        target_name=None,
        ra_deg=ra,
        dec_deg=-20.0,
        s_fov_deg=1.0 / 3600.0,
        s_region=None,
        search_radius_deg=1.0 / 3600.0,
        instrument_name=None,
        access_url=None,
        healpix_order10=healpix_order10(ra, -20.0),
    )


def test_radius_uses_half_fov_with_floor() -> None:
    assert search_radius_deg(10.0 / 3600.0, 1.0) == pytest.approx(
        5.0 / 3600.0
    )
    assert search_radius_deg(1.0 / 3600.0, 1.0) == pytest.approx(
        1.0 / 3600.0
    )
    assert search_radius_deg(None, 1.0) == pytest.approx(1.0 / 3600.0)
    assert search_radius_deg(float("nan"), 1.0) == pytest.approx(
        1.0 / 3600.0
    )


@pytest.mark.parametrize(
    ("ra", "dec"),
    [(-1.0, 0.0), (360.0, 0.0), (0.0, -91.0), (0.0, 91.0)],
)
def test_invalid_coordinates_are_rejected(ra: float, dec: float) -> None:
    with pytest.raises(ValueError):
        validate_position(ra, dec)


def test_consolidation_keeps_all_observation_ids() -> None:
    targets = consolidate_search_targets(
        [observation("A"), observation("B"), observation("C", ra=11.0)]
    )
    assert len(targets) == 2
    assert targets[0].observation_ids == ("A", "B")
    assert targets[1].observation_ids == ("C",)
    assert batch_hash("ned", targets) == batch_hash("ned", targets)
    assert batch_hash("ned", targets) != batch_hash("simbad", targets)


def test_healpix_is_deterministic_and_in_range() -> None:
    value = healpix_order10(10.0, -20.0)
    assert value == healpix_order10(10.0, -20.0)
    assert 0 <= value < 12 * 1024**2

