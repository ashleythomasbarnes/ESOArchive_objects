from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlparse

from astropy.table import Table
from astroquery.simbad import Simbad
from pyvo.dal import TAPService

from .geometry import healpix_order10, search_radius_deg, validate_position
from .models import (
    BatchResult,
    CatalogAlias,
    CatalogObject,
    ObjectMatch,
    Observation,
    SearchTarget,
)


def clean_value(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "mask") and bool(getattr(value, "mask", False)):
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def clean_float(value: Any) -> float | None:
    value = clean_value(value)
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def row_value(row: Any, name: str) -> Any:
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


def build_eso_query(limit: int) -> str:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return f"""
        SELECT TOP {int(limit)}
            dp_id, obs_publisher_did, target_name, s_ra, s_dec, s_fov,
            s_region, instrument_name, access_url
        FROM ivoa.ObsCore
        WHERE dataproduct_type = 'spectrum'
          AND calib_level >= 2
          AND s_ra IS NOT NULL
          AND s_dec IS NOT NULL
        ORDER BY dp_id
    """


class EsoClient:
    def __init__(self, endpoint: str):
        self.service = TAPService(endpoint)

    def fetch_observations(
        self, limit: int, min_radius_arcsec: float
    ) -> list[Observation]:
        table = self.service.search(build_eso_query(limit)).to_table()
        observations = []
        for row in table:
            ra_deg = float(row_value(row, "s_ra"))
            dec_deg = float(row_value(row, "s_dec"))
            validate_position(ra_deg, dec_deg)
            s_fov_deg = clean_float(row_value(row, "s_fov"))
            observations.append(
                Observation(
                    eso_dp_id=str(clean_value(row_value(row, "dp_id"))),
                    obs_publisher_did=clean_value(
                        row_value(row, "obs_publisher_did")
                    ),
                    target_name=clean_value(row_value(row, "target_name")),
                    ra_deg=ra_deg,
                    dec_deg=dec_deg,
                    s_fov_deg=s_fov_deg,
                    s_region=clean_value(row_value(row, "s_region")),
                    search_radius_deg=search_radius_deg(
                        s_fov_deg, min_radius_arcsec
                    ),
                    instrument_name=clean_value(
                        row_value(row, "instrument_name")
                    ),
                    access_url=clean_value(row_value(row, "access_url")),
                    healpix_order10=healpix_order10(ra_deg, dec_deg),
                )
            )
        return observations


def build_simbad_query() -> str:
    return """
        SELECT
            u.search_key,
            b.oid,
            b.main_id,
            b.ra,
            b.dec,
            b.otype,
            b.sp_type,
            b.morph_type,
            d.label,
            d.description,
            d.path,
            d.is_candidate,
            DISTANCE(
                POINT('ICRS', b.ra, b.dec),
                POINT('ICRS', u.ra_deg, u.dec_deg)
            ) * 3600.0 AS separation_arcsec
        FROM basic AS b
        JOIN TAP_UPLOAD.targets AS u
          ON 1 = CONTAINS(
              POINT('ICRS', b.ra, b.dec),
              CIRCLE('ICRS', u.ra_deg, u.dec_deg, u.radius_deg)
          )
        LEFT OUTER JOIN otypedef AS d
          ON b.otype = d.otype
    """


class SimbadClient:
    def __init__(self, endpoint: str):
        self.client = Simbad()
        parsed = urlparse(endpoint.rstrip("/"))
        expected_path = "/simbad/sim-tap"
        if parsed.scheme != "https" or parsed.path != expected_path:
            raise ValueError(
                "the Astroquery SIMBAD adapter requires an endpoint shaped as "
                f"https://<server>{expected_path}"
            )
        self.client.server = parsed.netloc

    def query_batch(self, targets: Sequence[SearchTarget]) -> BatchResult:
        upload = Table(
            rows=[
                (
                    target.search_key,
                    target.ra_deg,
                    target.dec_deg,
                    target.radius_deg,
                )
                for target in targets
            ],
            names=("search_key", "ra_deg", "dec_deg", "radius_deg"),
            dtype=("U32", "f8", "f8", "f8"),
        )
        table = self.client.query_tap(
            build_simbad_query(),
            maxrec=self.client.hardlimit,
            async_job=len(targets) > 1_000,
            targets=upload,
        )
        return map_simbad_results(targets, table)

    def query_aliases(
        self, object_ids: Sequence[str]
    ) -> tuple[CatalogAlias, ...]:
        if not object_ids:
            return ()
        upload = Table(
            rows=[(int(object_id),) for object_id in object_ids],
            names=("oid",),
            dtype=("i8",),
        )
        table = self.client.query_tap(
            build_simbad_alias_query(),
            maxrec=self.client.hardlimit,
            async_job=len(object_ids) > 10_000,
            objects=upload,
        )
        if table is None:
            return ()
        aliases = {
            (
                str(clean_value(row_value(row, "oidref"))),
                str(clean_value(row_value(row, "id"))),
            )
            for row in table
            if clean_value(row_value(row, "oidref")) is not None
            and clean_value(row_value(row, "id")) is not None
        }
        return tuple(
            CatalogAlias(
                catalog="simbad",
                catalog_object_id=object_id,
                alias=alias,
            )
            for object_id, alias in sorted(aliases)
        )


def build_simbad_alias_query() -> str:
    return """
        SELECT i.oidref, i.id
        FROM ident AS i
        JOIN TAP_UPLOAD.objects AS u
          ON i.oidref = u.oid
    """


def map_simbad_results(
    targets: Sequence[SearchTarget], table: Table | None
) -> BatchResult:
    by_key = {target.search_key: target for target in targets}
    objects: dict[str, CatalogObject] = {}
    matches: dict[tuple[str, str], ObjectMatch] = {}
    if table is None:
        return BatchResult((), ())
    for row in table:
        search_key = str(clean_value(row_value(row, "search_key")))
        target = by_key.get(search_key)
        if target is None:
            continue
        object_id = str(clean_value(row_value(row, "oid")))
        obj = CatalogObject(
            catalog="simbad",
            catalog_object_id=object_id,
            preferred_name=clean_value(row_value(row, "main_id")),
            ra_deg=float(row_value(row, "ra")),
            dec_deg=float(row_value(row, "dec")),
            primary_type_code=clean_value(row_value(row, "otype")),
            primary_type_label=clean_value(row_value(row, "label")),
            primary_type_description=clean_value(
                row_value(row, "description")
            ),
            primary_type_path=clean_value(row_value(row, "path")),
            primary_type_is_candidate=(
                None
                if clean_value(row_value(row, "is_candidate")) is None
                else bool(int(row_value(row, "is_candidate")))
            ),
            spectral_type=clean_value(row_value(row, "sp_type")),
            morphological_type=clean_value(row_value(row, "morph_type")),
        )
        objects[object_id] = obj
        separation = float(row_value(row, "separation_arcsec"))
        for observation_id in target.observation_ids:
            key = (observation_id, object_id)
            matches[key] = ObjectMatch(
                eso_dp_id=observation_id,
                catalog="simbad",
                catalog_object_id=object_id,
                separation_arcsec=separation,
            )
    return BatchResult(tuple(objects.values()), tuple(matches.values()))



ClientFactory = Callable[[str], Any]
