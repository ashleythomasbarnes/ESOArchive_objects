from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class RunConfig:
    limit: int = 50
    min_radius_arcsec: float = 1.0
    simbad_batch_size: int = 50_000
    ned_batch_size: int = 50
    retries: int = 5
    eso_endpoint: str = "https://archive.eso.org/tap_obs"
    simbad_endpoint: str = "https://simbad.cds.unistra.fr/simbad/sim-tap"
    ned_endpoint: str = "https://ned.ipac.caltech.edu/tap"
    output_dir: str = "output"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "RunConfig":
        allowed = cls.__dataclass_fields__
        return cls(**{key: value for key, value in values.items() if key in allowed})

    def validate(self) -> None:
        if self.limit < 1:
            raise ValueError("limit must be at least 1")
        if self.min_radius_arcsec <= 0:
            raise ValueError("min_radius_arcsec must be positive")
        if not 1 <= self.simbad_batch_size <= 200_000:
            raise ValueError("simbad_batch_size must be between 1 and 200000")
        if self.ned_batch_size < 1:
            raise ValueError("ned_batch_size must be at least 1")
        if self.retries < 1:
            raise ValueError("retries must be at least 1")


@dataclass(frozen=True)
class Observation:
    eso_dp_id: str
    obs_publisher_did: str | None
    target_name: str | None
    ra_deg: float
    dec_deg: float
    s_fov_deg: float | None
    s_region: str | None
    search_radius_deg: float
    instrument_name: str | None
    access_url: str | None
    healpix_order10: int


@dataclass(frozen=True)
class SearchTarget:
    search_key: str
    ra_deg: float
    dec_deg: float
    radius_deg: float
    observation_ids: tuple[str, ...]


@dataclass(frozen=True)
class CatalogObject:
    catalog: str
    catalog_object_id: str
    preferred_name: str | None
    ra_deg: float
    dec_deg: float
    primary_type_code: str | None
    primary_type_label: str | None
    primary_type_description: str | None
    catalog_type_key: str | None = None


@dataclass(frozen=True)
class ObjectMatch:
    eso_dp_id: str
    catalog: str
    catalog_object_id: str
    separation_arcsec: float


@dataclass(frozen=True)
class BatchResult:
    objects: tuple[CatalogObject, ...]
    matches: tuple[ObjectMatch, ...]

