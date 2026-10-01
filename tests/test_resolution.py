from __future__ import annotations

import argparse
import sqlite3

from eso_object_types.cli import report_command
from eso_object_types.database import Database
from eso_object_types.geometry import healpix_order10
from eso_object_types.models import (
    BatchResult,
    CatalogAlias,
    CatalogObject,
    ObjectMatch,
    Observation,
    RunConfig,
)
from eso_object_types.resolution import (
    normalize_name,
    rebuild_best_objects,
    target_name_variants,
)


def observation(target_name: str | None = "Target") -> Observation:
    return Observation(
        eso_dp_id="ESO-1",
        obs_publisher_did=None,
        target_name=target_name,
        ra_deg=10.0,
        dec_deg=-20.0,
        s_fov_deg=2.0 / 3600.0,
        s_region=None,
        search_radius_deg=1.0 / 3600.0,
        instrument_name="TEST",
        access_url=None,
        healpix_order10=healpix_order10(10.0, -20.0),
    )


def save_catalog(
    database: Database,
    catalog: str,
    objects: list[CatalogObject],
    separations: list[float],
) -> None:
    call_hash = f"{catalog}-batch"
    database.start_service_call("run-1", catalog, call_hash, 1, 1)
    database.complete_catalog_call(
        "run-1",
        catalog,
        call_hash,
        0.1,
        BatchResult(
            objects=tuple(objects),
            matches=tuple(
                ObjectMatch(
                    eso_dp_id="ESO-1",
                    catalog=catalog,
                    catalog_object_id=obj.catalog_object_id,
                    separation_arcsec=separation,
                )
                for obj, separation in zip(objects, separations, strict=True)
            ),
        ),
        ["ESO-1"],
    )


def save_aliases(
    database: Database, object_ids: list[str], aliases: list[CatalogAlias]
) -> None:
    database.start_service_call(
        "run-1", "simbad_alias", "alias-batch", 1, len(object_ids)
    )
    database.complete_alias_call(
        "run-1", "alias-batch", 0.1, object_ids, aliases
    )


def best_row(database: Database) -> sqlite3.Row:
    rebuild_best_objects(database, "run-1")
    return database.connection.execute(
        "SELECT * FROM observation_best_objects WHERE run_id = 'run-1'"
    ).fetchone()


def test_target_name_normalization_and_suffixes() -> None:
    assert normalize_name("NGC 253") == "ngc253"
    assert target_name_variants("NGC253_offset") == (
        "ngc253offset",
        "ngc253",
    )
    assert target_name_variants("NGC253_offset_north") == (
        "ngc253offsetnorth",
        "ngc253",
    )
    assert target_name_variants("NGC253_pointing2")[1] == "ngc253"
    assert target_name_variants("NGC253_pos2")[1] == "ngc253"
    assert target_name_variants("NGC253_mosaic3")[1] == "ngc253"
    assert target_name_variants("_offset") == (None, None)
    assert target_name_variants("NGC-253") == ("ngc253", None)
    assert target_name_variants("Target") == (None, None)
    assert target_name_variants("ordinary-name") == (
        "ordinaryname",
        None,
    )


def test_alias_match_selects_simbad_object(tmp_path) -> None:
    database = Database(tmp_path / "test.sqlite")
    try:
        database.create_run("run-1", RunConfig())
        database.save_observations("run-1", [observation("NGC224_offset")])
        save_catalog(
            database,
            "simbad",
            [
                CatalogObject(
                    catalog="simbad",
                    catalog_object_id="100",
                    preferred_name="M 31",
                    ra_deg=10.0,
                    dec_deg=-20.0,
                    primary_type_code="G",
                    primary_type_label="Galaxy",
                    primary_type_description="Galaxy",
                    primary_type_path="G",
                    primary_type_is_candidate=False,
                    morphological_type="SA(s)b",
                )
            ],
            [0.2],
        )
        save_aliases(
            database,
            ["100"],
            [
                CatalogAlias("simbad", "100", "M 31"),
                CatalogAlias("simbad", "100", "NGC 224"),
            ],
        )

        row = best_row(database)
        assert row["best_object_name"] == "M 31"
        assert row["broad_category"] == "Galaxy"
        assert row["classification_detail"] == "SA(s)b"
        assert row["match_method"] == "target_name_base"
        assert row["confidence"] == "high"
        assert row["supporting_catalogs"] == "simbad"
        assert row["candidate_group_count"] == 1
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM observation_best_object_members"
            ).fetchone()[0]
            == 1
        )
    finally:
        database.close()


def test_existing_alias_row_is_a_complete_cache_entry(tmp_path) -> None:
    database = Database(tmp_path / "test.sqlite")
    try:
        database.create_run("run-1", RunConfig())
        database.save_observations("run-1", [observation("NGC 224")])
        save_catalog(
            database,
            "simbad",
            [
                CatalogObject(
                    catalog="simbad",
                    catalog_object_id="100",
                    preferred_name="M 31",
                    ra_deg=10.0,
                    dec_deg=-20.0,
                    primary_type_code="G",
                    primary_type_label="Galaxy",
                    primary_type_description="Galaxy",
                    primary_type_path="G",
                )
            ],
            [0.2],
        )
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO catalog_object_aliases(
                    catalog, catalog_object_id, alias, normalized_alias, updated_at
                ) VALUES ('simbad', '100', 'NGC 224', 'ngc224', 'now')
                """
            )

        assert database.uncached_simbad_object_ids_for_run("run-1") == []
        row = best_row(database)
        assert row["best_object_name"] == "M 31"
        assert row["match_method"] == "target_name"
        assert row["confidence"] == "high"
        assert row["alias_complete"] == 1
    finally:
        database.close()


def test_shared_alias_selects_nearest_candidate(tmp_path) -> None:
    database = Database(tmp_path / "test.sqlite")
    try:
        database.create_run("run-1", RunConfig())
        database.save_observations("run-1", [observation("Shared")])
        save_catalog(
            database,
            "simbad",
            [
                CatalogObject(
                    catalog="simbad",
                    catalog_object_id="near",
                    preferred_name="SIMBAD A",
                    ra_deg=10.0,
                    dec_deg=-20.0,
                    primary_type_code="G",
                    primary_type_label="Galaxy",
                    primary_type_description="Galaxy",
                    primary_type_path="G",
                ),
                CatalogObject(
                    catalog="simbad",
                    catalog_object_id="far",
                    preferred_name="SIMBAD B",
                    ra_deg=10.00005,
                    dec_deg=-20.0,
                    primary_type_code="G",
                    primary_type_label="Galaxy",
                    primary_type_description="Galaxy",
                    primary_type_path="G",
                ),
            ],
            [0.1, 0.2],
        )
        save_aliases(
            database,
            ["near", "far"],
            [
                CatalogAlias("simbad", "near", "Shared"),
                CatalogAlias("simbad", "far", "Shared"),
            ],
        )

        row = best_row(database)
        assert row["candidate_group_count"] == 2
        members = database.connection.execute(
            """
            SELECT catalog, catalog_object_id
            FROM observation_best_object_members
            ORDER BY catalog
            """
        ).fetchall()
        assert [(item["catalog"], item["catalog_object_id"]) for item in members] == [
            ("simbad", "near"),
        ]
    finally:
        database.close()


def test_name_match_beats_nearer_unrelated_object(tmp_path) -> None:
    database = Database(tmp_path / "test.sqlite")
    try:
        database.create_run("run-1", RunConfig())
        database.save_observations("run-1", [observation("LSQ12dyw")])
        save_catalog(
            database,
            "simbad",
            [
                CatalogObject(
                    catalog="simbad",
                    catalog_object_id="sn",
                    preferred_name="LSQ 12dyw",
                    ra_deg=10.0,
                    dec_deg=-20.0,
                    primary_type_code="SN*",
                    primary_type_label="Supernova",
                    primary_type_description="SuperNova",
                    primary_type_path="* > SN*",
                    primary_type_is_candidate=False,
                ),
                CatalogObject(
                    catalog="simbad",
                    catalog_object_id="galaxy",
                    preferred_name="SDSS J0000",
                    ra_deg=10.0,
                    dec_deg=-20.0,
                    primary_type_code="G",
                    primary_type_label="Galaxy",
                    primary_type_description="Galaxy",
                    primary_type_path="G",
                ),
            ],
            [0.94, 0.10],
        )
        save_aliases(
            database,
            ["sn", "galaxy"],
            [
                CatalogAlias("simbad", "sn", "LSQ 12dyw"),
                CatalogAlias("simbad", "galaxy", "SDSS J0000"),
            ],
        )
        row = best_row(database)
        assert row["best_object_name"] == "LSQ 12dyw"
        assert row["broad_category"] == "Supernova"
        assert row["subcategory"] == "Unknown subtype"
        assert row["confidence"] == "high"
        assert row["candidate_group_count"] == 2
    finally:
        database.close()


def test_coordinate_only_neighbors_are_not_combined(tmp_path) -> None:
    database = Database(tmp_path / "test.sqlite")
    try:
        database.create_run("run-1", RunConfig())
        database.save_observations("run-1", [observation("_offset")])
        save_catalog(
            database,
            "simbad",
            [
                CatalogObject(
                    catalog="simbad",
                    catalog_object_id="star",
                    preferred_name="A star",
                    ra_deg=10.0,
                    dec_deg=-20.0,
                    primary_type_code="*",
                    primary_type_label="Star",
                    primary_type_description="Star",
                    primary_type_path="*",
                ),
                CatalogObject(
                    catalog="simbad",
                    catalog_object_id="galaxy",
                    preferred_name="A galaxy",
                    ra_deg=10.0,
                    dec_deg=-20.0,
                    primary_type_code="G",
                    primary_type_label="G",
                    primary_type_description=None,
                )
            ],
            [0.1, 0.2],
        )
        save_aliases(
            database,
            ["star", "galaxy"],
            [CatalogAlias("simbad", "star", "A star")],
        )
        row = best_row(database)
        assert row["best_object_name"] == "A star"
        assert row["candidate_group_count"] == 2
        assert row["supporting_catalogs"] == "simbad"
        assert row["match_method"] == "position"
    finally:
        database.close()


def test_single_and_clear_nearest_candidates_have_medium_confidence(
    tmp_path,
) -> None:
    single_database = Database(tmp_path / "single.sqlite")
    try:
        single_database.create_run("run-1", RunConfig())
        single_database.save_observations("run-1", [observation("_offset")])
        save_catalog(
            single_database,
            "simbad",
            [
                CatalogObject(
                    catalog="simbad",
                    catalog_object_id="single",
                    preferred_name="Only object",
                    ra_deg=10.0,
                    dec_deg=-20.0,
                    primary_type_code="*",
                    primary_type_label="Star",
                    primary_type_description="Star",
                    primary_type_path="*",
                )
            ],
            [0.8],
        )
        save_aliases(
            single_database,
            ["single"],
            [CatalogAlias("simbad", "single", "Only object")],
        )
        assert best_row(single_database)["confidence"] == "medium"
    finally:
        single_database.close()

    nearest_database = Database(tmp_path / "nearest.sqlite")
    try:
        nearest_database.create_run("run-1", RunConfig())
        nearest_database.save_observations("run-1", [observation("_offset")])
        save_catalog(
            nearest_database,
            "simbad",
            [
                CatalogObject(
                    catalog="simbad",
                    catalog_object_id=object_id,
                    preferred_name=object_id,
                    ra_deg=10.0,
                    dec_deg=-20.0,
                    primary_type_code="*",
                    primary_type_label="Star",
                    primary_type_description="Star",
                    primary_type_path="*",
                )
                for object_id in ("nearest", "runner-up")
            ],
            [0.1, 0.5],
        )
        save_aliases(
            nearest_database,
            ["nearest", "runner-up"],
            [
                CatalogAlias("simbad", "nearest", "nearest"),
                CatalogAlias("simbad", "runner-up", "runner-up"),
            ],
        )
        row = best_row(nearest_database)
        assert row["best_object_name"] == "nearest"
        assert row["confidence"] == "medium"
        assert row["runner_up_margin"] == 0.4
    finally:
        nearest_database.close()


def test_equal_position_candidates_resolve_deterministically(tmp_path) -> None:
    database = Database(tmp_path / "test.sqlite")
    try:
        database.create_run("run-1", RunConfig())
        database.save_observations("run-1", [observation("_offset")])
        save_catalog(
            database,
            "simbad",
            [
                CatalogObject(
                    catalog="simbad",
                    catalog_object_id=object_id,
                    preferred_name=object_id,
                    ra_deg=10.0,
                    dec_deg=-20.0,
                    primary_type_code="G",
                    primary_type_label="G",
                    primary_type_description=None,
                )
                for object_id in ("one", "two")
            ],
            [0.2, 0.2],
        )

        first = best_row(database)["best_object_key"]
        second = best_row(database)["best_object_key"]
        assert first == second
    finally:
        database.close()


def test_no_candidate_still_gets_one_best_row(tmp_path) -> None:
    database = Database(tmp_path / "test.sqlite")
    try:
        database.create_run("run-1", RunConfig())
        database.save_observations("run-1", [observation(None)])
        row = best_row(database)
        assert row["best_object_key"] is None
        assert row["broad_category"] == "Unknown"
        assert row["confidence"] == "none"
        assert row["candidate_group_count"] == 0
    finally:
        database.close()


def test_report_rebuilds_missing_best_rows_without_service_calls(tmp_path) -> None:
    path = tmp_path / "test.sqlite"
    database = Database(path)
    try:
        database.create_run("run-1", RunConfig())
        database.save_observations("run-1", [observation(None)])
        database.finish_run("run-1", "completed", {})
        assert database.connection.execute(
            "SELECT COUNT(*) FROM observation_best_objects"
        ).fetchone()[0] == 0
    finally:
        database.close()

    exit_code = report_command(
        argparse.Namespace(
            database=str(path),
            run_id="run-1",
            output_dir=str(tmp_path / "reports"),
        )
    )
    assert exit_code == 0
    rebuilt = Database(path)
    try:
        row = rebuilt.connection.execute(
            "SELECT * FROM observation_best_objects"
        ).fetchone()
        assert row["broad_category"] == "Unknown"
        assert row["confidence"] == "none"
        assert rebuilt.connection.execute(
            "SELECT COUNT(*) FROM service_calls"
        ).fetchone()[0] == 0
    finally:
        rebuilt.close()


def test_schema_v1_database_is_migrated_in_place(tmp_path) -> None:
    path = tmp_path / "old.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE object_types (
            catalog TEXT NOT NULL,
            type_code TEXT NOT NULL,
            type_label TEXT,
            type_description TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (catalog, type_code)
        );
        CREATE TABLE catalog_objects (
            catalog TEXT NOT NULL,
            catalog_object_id TEXT NOT NULL,
            preferred_name TEXT,
            ra_deg REAL NOT NULL,
            dec_deg REAL NOT NULL,
            primary_type_code TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (catalog, catalog_object_id),
            FOREIGN KEY (catalog, primary_type_code)
                REFERENCES object_types(catalog, type_code)
        );
        """
    )
    connection.close()

    database = Database(path)
    try:
        object_type_columns = {
            row["name"]
            for row in database.connection.execute("PRAGMA table_info(object_types)")
        }
        object_columns = {
            row["name"]
            for row in database.connection.execute(
                "PRAGMA table_info(catalog_objects)"
            )
        }
        assert {"type_path", "type_is_candidate"} <= object_type_columns
        assert {
            "spectral_type",
            "morphological_type",
            "aliases_retrieved_at",
        } <= object_columns
        assert database.connection.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        database.close()
