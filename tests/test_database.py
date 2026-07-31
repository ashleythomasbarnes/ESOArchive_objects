from __future__ import annotations

import sqlite3

import pytest

from eso_object_types.database import Database
from eso_object_types.geometry import healpix_order10
from eso_object_types.models import (
    BatchResult,
    CatalogObject,
    ObjectMatch,
    Observation,
    RunConfig,
)


def sample_observation() -> Observation:
    return Observation(
        eso_dp_id="ESO-1",
        obs_publisher_did="ivo://eso/1",
        target_name="Target",
        ra_deg=10.0,
        dec_deg=-20.0,
        s_fov_deg=1.0 / 3600.0,
        s_region="POSITION ICRS 10 -20",
        search_radius_deg=1.0 / 3600.0,
        instrument_name="TEST",
        access_url=None,
        healpix_order10=healpix_order10(10.0, -20.0),
    )


def test_idempotent_objects_links_and_catalog_separation(tmp_path) -> None:
    database = Database(tmp_path / "test.sqlite")
    try:
        database.create_run("run-1", RunConfig())
        database.save_observations("run-1", [sample_observation()])

        for catalog, object_id in (("simbad", "10"), ("ned", "10")):
            call_hash = f"{catalog}-batch"
            database.start_service_call("run-1", catalog, call_hash, 1, 1)
            result = BatchResult(
                objects=(
                    CatalogObject(
                        catalog=catalog,
                        catalog_object_id=object_id,
                        preferred_name="Target",
                        ra_deg=10.0,
                        dec_deg=-20.0,
                        primary_type_code="Star",
                        primary_type_label="Star",
                        primary_type_description=None,
                    ),
                ),
                matches=(
                    ObjectMatch(
                        eso_dp_id="ESO-1",
                        catalog=catalog,
                        catalog_object_id=object_id,
                        separation_arcsec=0.1,
                    ),
                ),
            )
            database.complete_catalog_call(
                "run-1", catalog, call_hash, 0.1, result, ["ESO-1"]
            )
            database.start_service_call("run-1", catalog, call_hash, 1, 1)
            database.complete_catalog_call(
                "run-1", catalog, call_hash, 0.1, result, ["ESO-1"]
            )

        assert database.connection.execute(
            "SELECT COUNT(*) FROM catalog_objects"
        ).fetchone()[0] == 2
        assert database.connection.execute(
            "SELECT COUNT(*) FROM observation_objects"
        ).fetchone()[0] == 2
    finally:
        database.close()


def test_foreign_keys_reject_orphan_links(tmp_path) -> None:
    database = Database(tmp_path / "test.sqlite")
    try:
        database.create_run("run-1", RunConfig())
        with pytest.raises(sqlite3.IntegrityError):
            with database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO observation_objects(
                        eso_dp_id, catalog, catalog_object_id, separation_arcsec,
                        first_seen_run_id, last_seen_run_id, updated_at
                    ) VALUES ('missing', 'ned', '1', 0, 'run-1', 'run-1', 'now')
                    """
                )
    finally:
        database.close()


def test_foreign_keys_reject_orphan_best_object_members(tmp_path) -> None:
    database = Database(tmp_path / "test.sqlite")
    try:
        database.create_run("run-1", RunConfig())
        with pytest.raises(sqlite3.IntegrityError):
            with database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO observation_best_object_members(
                        run_id, eso_dp_id, catalog, catalog_object_id, member_role
                    ) VALUES ('run-1', 'missing', 'ned', '1', 'primary')
                    """
                )
    finally:
        database.close()
