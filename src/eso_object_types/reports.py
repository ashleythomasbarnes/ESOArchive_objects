from __future__ import annotations

import csv
import math
import sqlite3
from pathlib import Path
from typing import Any


def _write_query_csv(
    connection: sqlite3.Connection,
    path: Path,
    query: str,
    parameters: tuple[Any, ...],
) -> int:
    cursor = connection.execute(query, parameters)
    rows = cursor.fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow([column[0] for column in cursor.description])
        writer.writerows(rows)
    return len(rows)


def build_summary(
    connection: sqlite3.Connection,
    run_id: str,
    simbad_batch_size: int,
    ned_batch_size: int,
) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    metrics["observations"] = connection.execute(
        "SELECT COUNT(*) FROM run_observations WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    metrics["unique_search_positions"] = connection.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT DISTINCT o.ra_deg, o.dec_deg, o.search_radius_deg
            FROM observations AS o
            JOIN run_observations AS r USING (eso_dp_id)
            WHERE r.run_id = ?
        )
        """,
        (run_id,),
    ).fetchone()[0]
    metrics["linked_observations"] = connection.execute(
        """
        SELECT COUNT(DISTINCT oo.eso_dp_id)
        FROM observation_objects AS oo
        JOIN run_observations AS r USING (eso_dp_id)
        WHERE r.run_id = ?
        """,
        (run_id,),
    ).fetchone()[0]
    metrics["zero_match_observations"] = (
        metrics["observations"] - metrics["linked_observations"]
    )
    metrics["catalog_objects"] = connection.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT DISTINCT oo.catalog, oo.catalog_object_id
            FROM observation_objects AS oo
            JOIN run_observations AS r USING (eso_dp_id)
            WHERE r.run_id = ?
        )
        """,
        (run_id,),
    ).fetchone()[0]
    metrics["observation_object_links"] = connection.execute(
        """
        SELECT COUNT(*)
        FROM observation_objects AS oo
        JOIN run_observations AS r USING (eso_dp_id)
        WHERE r.run_id = ?
        """,
        (run_id,),
    ).fetchone()[0]
    metrics["best_object_rows"] = connection.execute(
        """
        SELECT COUNT(*) FROM observation_best_objects WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()[0]
    metrics["best_object_matches"] = connection.execute(
        """
        SELECT COUNT(*) FROM observation_best_objects
        WHERE run_id = ? AND best_object_key IS NOT NULL
        """,
        (run_id,),
    ).fetchone()[0]
    metrics["best_object_cross_catalog"] = connection.execute(
        """
        SELECT COUNT(*) FROM observation_best_objects
        WHERE run_id = ? AND supporting_catalogs = 'ned,simbad'
        """,
        (run_id,),
    ).fetchone()[0]
    metrics["best_object_cross_catalog_agreement"] = connection.execute(
        """
        SELECT COUNT(*) FROM observation_best_objects
        WHERE run_id = ? AND supporting_catalogs = 'ned,simbad'
          AND classification_conflict = 0
        """,
        (run_id,),
    ).fetchone()[0]
    metrics["best_object_classification_conflicts"] = connection.execute(
        """
        SELECT COUNT(*) FROM observation_best_objects
        WHERE run_id = ? AND classification_conflict = 1
        """,
        (run_id,),
    ).fetchone()[0]
    metrics["best_object_incomplete_aliases"] = connection.execute(
        """
        SELECT COUNT(*) FROM observation_best_objects
        WHERE run_id = ? AND alias_complete = 0
        """,
        (run_id,),
    ).fetchone()[0]
    metrics["simbad_alias_objects"] = connection.execute(
        """
        SELECT COUNT(DISTINCT co.catalog_object_id)
        FROM catalog_objects AS co
        JOIN (
            SELECT DISTINCT oo.catalog, oo.catalog_object_id
            FROM observation_objects AS oo
            JOIN run_observations AS ro USING (eso_dp_id)
            WHERE ro.run_id = ?
        ) AS run_objects
          ON run_objects.catalog = co.catalog
         AND run_objects.catalog_object_id = co.catalog_object_id
        WHERE co.catalog = 'simbad'
          AND (
              co.aliases_retrieved_at IS NOT NULL
              OR EXISTS (
                  SELECT 1
                  FROM catalog_object_aliases AS cached_alias
                  WHERE cached_alias.catalog = co.catalog
                    AND cached_alias.catalog_object_id = co.catalog_object_id
              )
          )
        """,
        (run_id,),
    ).fetchone()[0]
    metrics["simbad_alias_rows"] = connection.execute(
        """
        SELECT COUNT(DISTINCT ca.catalog_object_id || ':' || ca.alias)
        FROM catalog_object_aliases AS ca
        JOIN (
            SELECT DISTINCT oo.catalog, oo.catalog_object_id
            FROM observation_objects AS oo
            JOIN run_observations AS ro USING (eso_dp_id)
            WHERE ro.run_id = ?
        ) AS run_objects
          ON run_objects.catalog = ca.catalog
         AND run_objects.catalog_object_id = ca.catalog_object_id
        """,
        (run_id,),
    ).fetchone()[0]

    calls = connection.execute(
        """
        SELECT service, status, COUNT(*) AS count,
               COALESCE(SUM(elapsed_seconds), 0) AS elapsed
        FROM service_calls
        WHERE run_id = ?
        GROUP BY service, status
        """,
        (run_id,),
    ).fetchall()
    for service, status, count, elapsed in calls:
        metrics[f"service_calls.{service}.{status}"] = count
        metrics[f"service_seconds.{service}.{status}"] = round(elapsed, 3)

    type_rows = connection.execute(
        """
        SELECT co.catalog, COALESCE(co.primary_type_code, 'UNKNOWN'), COUNT(*)
        FROM observation_objects AS oo
        JOIN run_observations AS r USING (eso_dp_id)
        JOIN catalog_objects AS co
          ON co.catalog = oo.catalog
         AND co.catalog_object_id = oo.catalog_object_id
        WHERE r.run_id = ?
        GROUP BY co.catalog, COALESCE(co.primary_type_code, 'UNKNOWN')
        ORDER BY co.catalog, COUNT(*) DESC
        """,
        (run_id,),
    ).fetchall()
    for catalog, type_code, count in type_rows:
        metrics[f"type_links.{catalog}.{type_code}"] = count

    for confidence, count in connection.execute(
        """
        SELECT confidence, COUNT(*)
        FROM observation_best_objects
        WHERE run_id = ?
        GROUP BY confidence
        """,
        (run_id,),
    ):
        metrics[f"best_confidence.{confidence}"] = count

    for category, count in connection.execute(
        """
        SELECT broad_category, COUNT(*)
        FROM observation_best_objects
        WHERE run_id = ?
        GROUP BY broad_category
        """,
        (run_id,),
    ):
        metrics[f"best_category.{category}"] = count

    observed = max(int(metrics["observations"]), 1)
    unique_fraction = metrics["unique_search_positions"] / observed
    projected_unique = math.ceil(2_000_000 * unique_fraction)
    metrics["scale.projected_unique_positions_for_2m"] = projected_unique
    metrics["scale.projected_simbad_batches_for_2m"] = math.ceil(
        projected_unique / simbad_batch_size
    )
    metrics["scale.projected_ned_batches_for_2m"] = math.ceil(
        projected_unique / ned_batch_size
    )
    metrics["scale.ned_public_tap_bulk_ready"] = "no"
    return metrics


def export_run(
    connection: sqlite3.Connection,
    run_id: str,
    output_dir: str | Path,
    simbad_batch_size: int,
    ned_batch_size: int,
) -> tuple[Path, dict[str, Any]]:
    destination = Path(output_dir) / run_id
    destination.mkdir(parents=True, exist_ok=True)

    exports = {
        "observations.csv": """
            SELECT o.*
            FROM observations AS o
            JOIN run_observations AS r USING (eso_dp_id)
            WHERE r.run_id = ?
            ORDER BY o.eso_dp_id
        """,
        "observation_objects.csv": """
            SELECT oo.*
            FROM observation_objects AS oo
            JOIN run_observations AS r USING (eso_dp_id)
            WHERE r.run_id = ?
            ORDER BY oo.eso_dp_id, oo.catalog, oo.catalog_object_id
        """,
        "catalog_objects.csv": """
            SELECT DISTINCT co.*
            FROM catalog_objects AS co
            JOIN observation_objects AS oo
              ON oo.catalog = co.catalog
             AND oo.catalog_object_id = co.catalog_object_id
            JOIN run_observations AS r USING (eso_dp_id)
            WHERE r.run_id = ?
            ORDER BY co.catalog, co.catalog_object_id
        """,
        "object_types.csv": """
            SELECT DISTINCT ot.*
            FROM object_types AS ot
            JOIN catalog_objects AS co
              ON co.catalog = ot.catalog
             AND co.primary_type_code = ot.type_code
            JOIN observation_objects AS oo
              ON oo.catalog = co.catalog
             AND oo.catalog_object_id = co.catalog_object_id
            JOIN run_observations AS r USING (eso_dp_id)
            WHERE r.run_id = ?
            ORDER BY ot.catalog, ot.type_code
        """,
        "observation_best_objects.csv": """
            SELECT o.target_name, bo.*
            FROM observation_best_objects AS bo
            JOIN observations AS o USING (eso_dp_id)
            WHERE bo.run_id = ?
            ORDER BY bo.eso_dp_id
        """,
        "observation_best_object_members.csv": """
            SELECT bom.*
            FROM observation_best_object_members AS bom
            WHERE bom.run_id = ?
            ORDER BY bom.eso_dp_id, bom.member_role, bom.catalog,
                     bom.catalog_object_id
        """,
        "catalog_object_aliases.csv": """
            SELECT ca.*
            FROM catalog_object_aliases AS ca
            JOIN (
                SELECT DISTINCT oo.catalog, oo.catalog_object_id
                FROM observation_objects AS oo
                JOIN run_observations AS ro USING (eso_dp_id)
                WHERE ro.run_id = ?
            ) AS run_objects
              ON run_objects.catalog = ca.catalog
             AND run_objects.catalog_object_id = ca.catalog_object_id
            ORDER BY ca.catalog, ca.catalog_object_id, ca.alias
        """,
    }
    for filename, query in exports.items():
        _write_query_csv(connection, destination / filename, query, (run_id,))

    summary = build_summary(
        connection, run_id, simbad_batch_size, ned_batch_size
    )
    with (destination / "run_summary.csv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.writer(stream)
        writer.writerow(["metric", "value"])
        writer.writerows(sorted(summary.items()))
    return destination, summary
