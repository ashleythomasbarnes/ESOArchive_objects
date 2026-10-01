from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import (
    BatchResult,
    BestObject,
    BestObjectMember,
    CatalogAlias,
    Observation,
    RunConfig,
)

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'partial')),
    config_json TEXT NOT NULL,
    summary_json TEXT
);

CREATE TABLE IF NOT EXISTS observations (
    eso_dp_id TEXT PRIMARY KEY,
    obs_publisher_did TEXT,
    target_name TEXT,
    ra_deg REAL NOT NULL,
    dec_deg REAL NOT NULL,
    s_fov_deg REAL,
    s_region TEXT,
    search_radius_deg REAL NOT NULL,
    instrument_name TEXT,
    access_url TEXT,
    healpix_order10 INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_observations (
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
    eso_dp_id TEXT NOT NULL REFERENCES observations(eso_dp_id) ON DELETE CASCADE,
    PRIMARY KEY (run_id, eso_dp_id)
);

CREATE TABLE IF NOT EXISTS object_types (
    catalog TEXT NOT NULL,
    type_code TEXT NOT NULL,
    type_label TEXT,
    type_description TEXT,
    type_path TEXT,
    type_is_candidate INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (catalog, type_code)
);

CREATE TABLE IF NOT EXISTS catalog_objects (
    catalog TEXT NOT NULL,
    catalog_object_id TEXT NOT NULL,
    preferred_name TEXT,
    ra_deg REAL NOT NULL,
    dec_deg REAL NOT NULL,
    primary_type_code TEXT,
    spectral_type TEXT,
    morphological_type TEXT,
    aliases_retrieved_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (catalog, catalog_object_id),
    FOREIGN KEY (catalog, primary_type_code)
        REFERENCES object_types(catalog, type_code)
);

CREATE TABLE IF NOT EXISTS observation_objects (
    eso_dp_id TEXT NOT NULL REFERENCES observations(eso_dp_id) ON DELETE CASCADE,
    catalog TEXT NOT NULL,
    catalog_object_id TEXT NOT NULL,
    separation_arcsec REAL NOT NULL,
    first_seen_run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    last_seen_run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (eso_dp_id, catalog, catalog_object_id),
    FOREIGN KEY (catalog, catalog_object_id)
        REFERENCES catalog_objects(catalog, catalog_object_id)
);

CREATE TABLE IF NOT EXISTS catalog_object_aliases (
    catalog TEXT NOT NULL,
    catalog_object_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (catalog, catalog_object_id, alias),
    FOREIGN KEY (catalog, catalog_object_id)
        REFERENCES catalog_objects(catalog, catalog_object_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS observation_best_objects (
    run_id TEXT NOT NULL,
    eso_dp_id TEXT NOT NULL,
    best_object_key TEXT,
    best_object_name TEXT,
    broad_category TEXT NOT NULL,
    subcategory TEXT,
    classification_detail TEXT,
    confidence TEXT NOT NULL
        CHECK (confidence IN ('high', 'medium', 'low', 'none')),
    match_method TEXT NOT NULL,
    target_name_variant TEXT,
    separation_arcsec REAL,
    normalized_separation REAL,
    candidate_group_count INTEGER NOT NULL,
    runner_up_margin REAL,
    supporting_catalogs TEXT,
    raw_catalog_types TEXT,
    alias_complete INTEGER NOT NULL,
    ranking_version TEXT NOT NULL,
    taxonomy_version TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, eso_dp_id),
    FOREIGN KEY (run_id, eso_dp_id)
        REFERENCES run_observations(run_id, eso_dp_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS observation_best_object_members (
    run_id TEXT NOT NULL,
    eso_dp_id TEXT NOT NULL,
    catalog TEXT NOT NULL,
    catalog_object_id TEXT NOT NULL,
    member_role TEXT NOT NULL CHECK (member_role IN ('primary', 'supporting')),
    PRIMARY KEY (run_id, eso_dp_id, catalog, catalog_object_id),
    FOREIGN KEY (run_id, eso_dp_id)
        REFERENCES observation_best_objects(run_id, eso_dp_id) ON DELETE CASCADE,
    FOREIGN KEY (catalog, catalog_object_id)
        REFERENCES catalog_objects(catalog, catalog_object_id)
);

CREATE TABLE IF NOT EXISTS service_calls (
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
    service TEXT NOT NULL,
    batch_hash TEXT NOT NULL,
    batch_number INTEGER NOT NULL,
    input_count INTEGER NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    result_count INTEGER,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    elapsed_seconds REAL,
    error_type TEXT,
    error_message TEXT,
    PRIMARY KEY (run_id, service, batch_hash)
);

CREATE INDEX IF NOT EXISTS idx_observations_hpx10
    ON observations(healpix_order10);
CREATE INDEX IF NOT EXISTS idx_catalog_objects_type
    ON catalog_objects(catalog, primary_type_code);
CREATE INDEX IF NOT EXISTS idx_observation_objects_reverse
    ON observation_objects(catalog, catalog_object_id);
CREATE INDEX IF NOT EXISTS idx_catalog_object_aliases_normalized
    ON catalog_object_aliases(normalized_alias);
CREATE INDEX IF NOT EXISTS idx_best_objects_category
    ON observation_best_objects(run_id, broad_category);
CREATE INDEX IF NOT EXISTS idx_best_members_catalog
    ON observation_best_object_members(catalog, catalog_object_id);
CREATE INDEX IF NOT EXISTS idx_run_observations_observation
    ON run_observations(eso_dp_id);
CREATE INDEX IF NOT EXISTS idx_service_calls_status
    ON service_calls(run_id, service, status);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        expected_columns = set(BestObject.__dataclass_fields__) | {"updated_at"}
        existing_columns = self._column_names("observation_best_objects")
        if existing_columns and existing_columns != expected_columns:
            self.connection.close()
            raise ValueError(
                f"database {self.path} has an incompatible best-object schema; "
                "use a fresh database with --database output/simbad_only.sqlite"
            )
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.executescript(SCHEMA)
        self._migrate_schema()
        self.connection.commit()

    def _column_names(self, table: str) -> set[str]:
        return {
            str(row["name"])
            for row in self.connection.execute(f"PRAGMA table_info({table})")
        }

    def _migrate_schema(self) -> None:
        migrations = {
            "object_types": {
                "type_path": "TEXT",
                "type_is_candidate": "INTEGER",
            },
            "catalog_objects": {
                "spectral_type": "TEXT",
                "morphological_type": "TEXT",
                "aliases_retrieved_at": "TEXT",
            },
        }
        for table, columns in migrations.items():
            existing = self._column_names(table)
            for column, declaration in columns.items():
                if column not in existing:
                    self.connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
                    )
        self.connection.execute("PRAGMA user_version = 2")

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self):
        try:
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def create_run(self, run_id: str, config: RunConfig) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO pipeline_runs(run_id, started_at, status, config_json)
                VALUES (?, ?, 'running', ?)
                """,
                (run_id, utc_now(), json.dumps(config.to_dict(), sort_keys=True)),
            )

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM pipeline_runs WHERE run_id = ?", (run_id,)
        ).fetchone()

    def latest_run_id(self) -> str | None:
        row = self.connection.execute(
            "SELECT run_id FROM pipeline_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return None if row is None else str(row["run_id"])

    def finish_run(self, run_id: str, status: str, summary: dict[str, Any]) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE pipeline_runs
                SET finished_at = ?, status = ?, summary_json = ?
                WHERE run_id = ?
                """,
                (utc_now(), status, json.dumps(summary, sort_keys=True), run_id),
            )

    def save_observations(
        self, run_id: str, observations: Iterable[Observation]
    ) -> None:
        now = utc_now()
        with self.transaction() as connection:
            for observation in observations:
                connection.execute(
                    """
                    INSERT INTO observations(
                        eso_dp_id, obs_publisher_did, target_name, ra_deg, dec_deg,
                        s_fov_deg, s_region, search_radius_deg, instrument_name,
                        access_url, healpix_order10, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(eso_dp_id) DO UPDATE SET
                        obs_publisher_did = excluded.obs_publisher_did,
                        target_name = excluded.target_name,
                        ra_deg = excluded.ra_deg,
                        dec_deg = excluded.dec_deg,
                        s_fov_deg = excluded.s_fov_deg,
                        s_region = excluded.s_region,
                        search_radius_deg = excluded.search_radius_deg,
                        instrument_name = excluded.instrument_name,
                        access_url = excluded.access_url,
                        healpix_order10 = excluded.healpix_order10,
                        updated_at = excluded.updated_at
                    """,
                    (
                        observation.eso_dp_id,
                        observation.obs_publisher_did,
                        observation.target_name,
                        observation.ra_deg,
                        observation.dec_deg,
                        observation.s_fov_deg,
                        observation.s_region,
                        observation.search_radius_deg,
                        observation.instrument_name,
                        observation.access_url,
                        observation.healpix_order10,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO run_observations(run_id, eso_dp_id)
                    VALUES (?, ?)
                    """,
                    (run_id, observation.eso_dp_id),
                )

    def observations_for_run(self, run_id: str) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                SELECT o.*
                FROM observations AS o
                JOIN run_observations AS r USING (eso_dp_id)
                WHERE r.run_id = ?
                ORDER BY o.eso_dp_id
                """,
                (run_id,),
            )
        )

    def service_call_status(
        self, run_id: str, service: str, batch_hash: str
    ) -> str | None:
        row = self.connection.execute(
            """
            SELECT status FROM service_calls
            WHERE run_id = ? AND service = ? AND batch_hash = ?
            """,
            (run_id, service, batch_hash),
        ).fetchone()
        return None if row is None else str(row["status"])

    def start_service_call(
        self,
        run_id: str,
        service: str,
        batch_hash: str,
        batch_number: int,
        input_count: int,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO service_calls(
                    run_id, service, batch_hash, batch_number, input_count,
                    attempt_count, status, started_at
                ) VALUES (?, ?, ?, ?, ?, 0, 'running', ?)
                ON CONFLICT(run_id, service, batch_hash) DO UPDATE SET
                    status = 'running',
                    started_at = excluded.started_at,
                    finished_at = NULL,
                    elapsed_seconds = NULL,
                    error_type = NULL,
                    error_message = NULL
                """,
                (
                    run_id,
                    service,
                    batch_hash,
                    batch_number,
                    input_count,
                    utc_now(),
                ),
            )

    def increment_service_attempt(
        self, run_id: str, service: str, batch_hash: str
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE service_calls SET attempt_count = attempt_count + 1
                WHERE run_id = ? AND service = ? AND batch_hash = ?
                """,
                (run_id, service, batch_hash),
            )

    def fail_service_call(
        self,
        run_id: str,
        service: str,
        batch_hash: str,
        elapsed_seconds: float,
        error: Exception,
    ) -> None:
        message = str(error).replace("\n", " ")[:1000]
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE service_calls
                SET status = 'failed', finished_at = ?, elapsed_seconds = ?,
                    error_type = ?, error_message = ?
                WHERE run_id = ? AND service = ? AND batch_hash = ?
                """,
                (
                    utc_now(),
                    elapsed_seconds,
                    type(error).__name__,
                    message,
                    run_id,
                    service,
                    batch_hash,
                ),
            )

    def complete_eso_call(
        self,
        run_id: str,
        batch_hash: str,
        elapsed_seconds: float,
        observations: Sequence[Observation],
    ) -> None:
        self.save_observations(run_id, observations)
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE service_calls
                SET status = 'completed', finished_at = ?, elapsed_seconds = ?,
                    result_count = ?, error_type = NULL, error_message = NULL
                WHERE run_id = ? AND service = 'eso' AND batch_hash = ?
                """,
                (utc_now(), elapsed_seconds, len(observations), run_id, batch_hash),
            )

    def complete_catalog_call(
        self,
        run_id: str,
        service: str,
        batch_hash: str,
        elapsed_seconds: float,
        result: BatchResult,
        observation_ids: Sequence[str],
    ) -> None:
        now = utc_now()
        with self.transaction() as connection:
            placeholders = ",".join("?" for _ in observation_ids)
            if observation_ids:
                connection.execute(
                    f"""
                    DELETE FROM observation_objects
                    WHERE catalog = ? AND eso_dp_id IN ({placeholders})
                    """,
                    (service, *observation_ids),
                )

            for obj in result.objects:
                if obj.primary_type_code:
                    connection.execute(
                        """
                        INSERT INTO object_types(
                            catalog, type_code, type_label, type_description,
                            type_path, type_is_candidate, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(catalog, type_code) DO UPDATE SET
                            type_label = excluded.type_label,
                            type_description = excluded.type_description,
                            type_path = excluded.type_path,
                            type_is_candidate = excluded.type_is_candidate,
                            updated_at = excluded.updated_at
                        """,
                        (
                            obj.catalog,
                            obj.primary_type_code,
                            obj.primary_type_label,
                            obj.primary_type_description,
                            obj.primary_type_path,
                            (
                                None
                                if obj.primary_type_is_candidate is None
                                else int(obj.primary_type_is_candidate)
                            ),
                            now,
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO catalog_objects(
                        catalog, catalog_object_id, preferred_name, ra_deg, dec_deg,
                        primary_type_code, spectral_type,
                        morphological_type, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(catalog, catalog_object_id) DO UPDATE SET
                        preferred_name = excluded.preferred_name,
                        ra_deg = excluded.ra_deg,
                        dec_deg = excluded.dec_deg,
                        primary_type_code = excluded.primary_type_code,
                        spectral_type = excluded.spectral_type,
                        morphological_type = excluded.morphological_type,
                        updated_at = excluded.updated_at
                    """,
                    (
                        obj.catalog,
                        obj.catalog_object_id,
                        obj.preferred_name,
                        obj.ra_deg,
                        obj.dec_deg,
                        obj.primary_type_code,
                        obj.spectral_type,
                        obj.morphological_type,
                        now,
                    ),
                )

            for match in result.matches:
                connection.execute(
                    """
                    INSERT INTO observation_objects(
                        eso_dp_id, catalog, catalog_object_id, separation_arcsec,
                        first_seen_run_id, last_seen_run_id, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(eso_dp_id, catalog, catalog_object_id) DO UPDATE SET
                        separation_arcsec = excluded.separation_arcsec,
                        last_seen_run_id = excluded.last_seen_run_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        match.eso_dp_id,
                        match.catalog,
                        match.catalog_object_id,
                        match.separation_arcsec,
                        run_id,
                        run_id,
                        now,
                    ),
                )

            connection.execute(
                """
                UPDATE service_calls
                SET status = 'completed', finished_at = ?, elapsed_seconds = ?,
                    result_count = ?, error_type = NULL, error_message = NULL
                WHERE run_id = ? AND service = ? AND batch_hash = ?
                """,
                (
                    utc_now(),
                    elapsed_seconds,
                    len(result.matches),
                    run_id,
                    service,
                    batch_hash,
                ),
            )

    def uncached_simbad_object_ids_for_run(self, run_id: str) -> list[str]:
        return [
            str(row[0])
            for row in self.connection.execute(
                """
                SELECT DISTINCT co.catalog_object_id
                FROM catalog_objects AS co
                JOIN observation_objects AS oo
                  ON oo.catalog = co.catalog
                 AND oo.catalog_object_id = co.catalog_object_id
                JOIN run_observations AS ro USING (eso_dp_id)
                WHERE ro.run_id = ?
                  AND co.catalog = 'simbad'
                  AND co.aliases_retrieved_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM catalog_object_aliases AS ca
                      WHERE ca.catalog = co.catalog
                        AND ca.catalog_object_id = co.catalog_object_id
                  )
                ORDER BY co.catalog_object_id
                """,
                (run_id,),
            )
        ]

    def complete_alias_call(
        self,
        run_id: str,
        call_hash: str,
        elapsed_seconds: float,
        object_ids: Sequence[str],
        aliases: Sequence[CatalogAlias],
    ) -> None:
        now = utc_now()
        with self.transaction() as connection:
            for object_id in object_ids:
                connection.execute(
                    """
                    DELETE FROM catalog_object_aliases
                    WHERE catalog = 'simbad' AND catalog_object_id = ?
                    """,
                    (object_id,),
                )
            for alias in aliases:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO catalog_object_aliases(
                        catalog, catalog_object_id, alias, normalized_alias, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        alias.catalog,
                        alias.catalog_object_id,
                        alias.alias,
                        "".join(
                            character
                            for character in alias.alias.casefold()
                            if character.isalnum()
                        ),
                        now,
                    ),
                )
            if object_ids:
                placeholders = ",".join("?" for _ in object_ids)
                connection.execute(
                    f"""
                    UPDATE catalog_objects
                    SET aliases_retrieved_at = ?
                    WHERE catalog = 'simbad'
                      AND catalog_object_id IN ({placeholders})
                    """,
                    (now, *object_ids),
                )
            connection.execute(
                """
                UPDATE service_calls
                SET status = 'completed', finished_at = ?, elapsed_seconds = ?,
                    result_count = ?, error_type = NULL, error_message = NULL
                WHERE run_id = ? AND service = 'simbad_alias'
                  AND batch_hash = ?
                """,
                (
                    utc_now(),
                    elapsed_seconds,
                    len(aliases),
                    run_id,
                    call_hash,
                ),
            )

    def replace_best_objects(
        self,
        run_id: str,
        best_objects: Sequence[BestObject],
        members: Sequence[BestObjectMember],
    ) -> None:
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM observation_best_objects WHERE run_id = ?",
                (run_id,),
            )
            for best in best_objects:
                connection.execute(
                    """
                    INSERT INTO observation_best_objects(
                        run_id, eso_dp_id, best_object_key, best_object_name,
                        broad_category, subcategory, classification_detail,
                        confidence, match_method, target_name_variant,
                        separation_arcsec, normalized_separation,
                        candidate_group_count, runner_up_margin,
                        supporting_catalogs, raw_catalog_types,
                        alias_complete,
                        ranking_version, taxonomy_version, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        best.run_id,
                        best.eso_dp_id,
                        best.best_object_key,
                        best.best_object_name,
                        best.broad_category,
                        best.subcategory,
                        best.classification_detail,
                        best.confidence,
                        best.match_method,
                        best.target_name_variant,
                        best.separation_arcsec,
                        best.normalized_separation,
                        best.candidate_group_count,
                        best.runner_up_margin,
                        best.supporting_catalogs,
                        best.raw_catalog_types,
                        int(best.alias_complete),
                        best.ranking_version,
                        best.taxonomy_version,
                        now,
                    ),
                )
            for member in members:
                connection.execute(
                    """
                    INSERT INTO observation_best_object_members(
                        run_id, eso_dp_id, catalog, catalog_object_id, member_role
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        member.run_id,
                        member.eso_dp_id,
                        member.catalog,
                        member.catalog_object_id,
                        member.member_role,
                    ),
                )
