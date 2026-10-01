from __future__ import annotations

import csv
import json

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
from eso_object_types.pipeline import Pipeline


def make_observation(index: int) -> Observation:
    ra = 10.0 + index
    return Observation(
        eso_dp_id=f"ESO-{index}",
        obs_publisher_did=f"ivo://eso/{index}",
        target_name=f"Target {index}",
        ra_deg=ra,
        dec_deg=-20.0,
        s_fov_deg=1.0 / 3600.0,
        s_region=None,
        search_radius_deg=1.0 / 3600.0,
        instrument_name="TEST",
        access_url=None,
        healpix_order10=healpix_order10(ra, -20.0),
    )


class FakeEso:
    calls = 0

    def __init__(self, endpoint: str):
        self.endpoint = endpoint

    def fetch_observations(self, limit: int, min_radius_arcsec: float):
        type(self).calls += 1
        return [make_observation(index) for index in range(limit)]


class FakeCatalog:
    catalog = "simbad"
    calls = 0

    def __init__(self, endpoint: str):
        self.endpoint = endpoint

    def query_batch(self, targets):
        type(self).calls += 1
        objects = []
        matches = []
        for target in targets:
            object_id = target.search_key
            objects.append(
                CatalogObject(
                    catalog=self.catalog,
                    catalog_object_id=object_id,
                    preferred_name=object_id,
                    ra_deg=target.ra_deg,
                    dec_deg=target.dec_deg,
                    primary_type_code="TEST",
                    primary_type_label="Test object",
                    primary_type_description=None,
                )
            )
            for observation_id in target.observation_ids:
                matches.append(
                    ObjectMatch(
                        eso_dp_id=observation_id,
                        catalog=self.catalog,
                        catalog_object_id=object_id,
                        separation_arcsec=0.0,
                    )
                )
        return BatchResult(tuple(objects), tuple(matches))


class FakeSimbad(FakeCatalog):
    catalog = "simbad"
    calls = 0
    alias_calls = 0

    def query_aliases(self, object_ids):
        type(self).alias_calls += 1
        return tuple(
            CatalogAlias("simbad", object_id, object_id)
            for object_id in object_ids
        )


class FailingSimbad(FakeSimbad):
    calls = 0

    def query_batch(self, targets):
        if targets[0].ra_deg == 12.0:
            type(self).calls += 1
            raise TimeoutError("simulated timeout")
        return super().query_batch(targets)


class FailingAliasSimbad(FakeSimbad):
    calls = 0
    alias_calls = 0

    def query_aliases(self, object_ids):
        type(self).alias_calls += 1
        raise TimeoutError("simulated alias timeout")


def test_pipeline_batches_exports_and_is_idempotent(tmp_path) -> None:
    FakeEso.calls = FakeSimbad.calls = 0
    FakeSimbad.alias_calls = 0
    database = Database(tmp_path / "prototype.sqlite")
    config = RunConfig(
        limit=3,
        simbad_batch_size=2,
        retries=2,
        output_dir=str(tmp_path / "output"),
    )
    try:
        pipeline = Pipeline(
            database,
            config,
            eso_factory=FakeEso,
            simbad_factory=FakeSimbad,
            sleep=lambda _: None,
            random_source=lambda: 0.0,
        )
        run_id, code, summary = pipeline.run()
        assert code == 0
        assert summary["observations"] == 3
        assert summary["observation_object_links"] == 3
        assert FakeEso.calls == 1
        assert FakeSimbad.calls == 2
        assert FakeSimbad.alias_calls == 1

        run_id_again, code, _ = pipeline.run(resume_run=run_id)
        assert run_id_again == run_id
        assert code == 0
        assert FakeEso.calls == 1
        assert FakeSimbad.calls == 2
        assert FakeSimbad.alias_calls == 1

        export_dir = tmp_path / "output" / run_id
        assert (export_dir / "observations.csv").exists()
        assert (export_dir / "catalog_objects.csv").exists()
        assert (export_dir / "object_types.csv").exists()
        assert (export_dir / "observation_objects.csv").exists()
        assert (export_dir / "observation_best_objects.csv").exists()
        assert (export_dir / "observation_best_object_members.csv").exists()
        assert (export_dir / "catalog_object_aliases.csv").exists()
        assert (export_dir / "run_summary.csv").exists()
        assert summary["best_object_rows"] == 3
        assert {
            row[0] for row in database.connection.execute(
                "SELECT DISTINCT catalog FROM catalog_objects"
            )
        } == {"simbad"}
        assert {
            row[0] for row in database.connection.execute(
                "SELECT DISTINCT service FROM service_calls"
            )
        } == {"eso", "simbad", "simbad_alias"}
        with (export_dir / "observation_best_objects.csv").open(
            newline="", encoding="utf-8"
        ) as stream:
            best_rows = list(csv.DictReader(stream))
        assert len(best_rows) == 3
        assert all(row["supporting_catalogs"] == "simbad" for row in best_rows)
        assert {
            "target_name",
            "best_object_key",
            "best_object_name",
            "broad_category",
            "subcategory",
            "classification_detail",
            "confidence",
            "match_method",
            "separation_arcsec",
            "normalized_separation",
            "candidate_group_count",
            "runner_up_margin",
            "supporting_catalogs",
            "raw_catalog_types",
            "ranking_version",
            "taxonomy_version",
        } <= set(best_rows[0])
        with (export_dir / "observation_best_object_members.csv").open(
            newline="", encoding="utf-8"
        ) as stream:
            member_rows = list(csv.DictReader(stream))
        assert {
            "run_id",
            "eso_dp_id",
            "catalog",
            "catalog_object_id",
            "member_role",
        } <= set(member_rows[0])
        assert len(member_rows) == 3
        assert all(row["catalog"] == "simbad" for row in member_rows)
        assert all(row["member_role"] == "primary" for row in member_rows)
        with (export_dir / "catalog_object_aliases.csv").open(
            newline="", encoding="utf-8"
        ) as stream:
            alias_rows = list(csv.DictReader(stream))
        assert {
            "catalog",
            "catalog_object_id",
            "alias",
            "normalized_alias",
            "updated_at",
        } <= set(alias_rows[0])
        log_lines = (tmp_path / "output" / "logs" / f"{run_id}.jsonl").read_text()
        assert '"event": "batch_complete"' in log_lines
        assert '"event": "batch_skip"' in log_lines
    finally:
        database.close()


def test_partial_run_resumes_only_failed_batches(tmp_path) -> None:
    FakeEso.calls = FakeSimbad.calls = FailingSimbad.calls = 0
    FakeSimbad.alias_calls = 0
    database = Database(tmp_path / "prototype.sqlite")
    config = RunConfig(
        limit=3,
        simbad_batch_size=2,
        retries=2,
        output_dir=str(tmp_path / "output"),
    )
    try:
        first = Pipeline(
            database,
            config,
            eso_factory=FakeEso,
            simbad_factory=FailingSimbad,
            sleep=lambda _: None,
            random_source=lambda: 0.0,
        )
        run_id, code, _ = first.run()
        assert code == 2
        assert FailingSimbad.calls == 3
        assert FakeSimbad.calls == 0
        assert database.get_run(run_id)["status"] == "partial"

        resumed = Pipeline(
            database,
            config,
            eso_factory=FakeEso,
            simbad_factory=FakeSimbad,
            sleep=lambda _: None,
            random_source=lambda: 0.0,
        )
        _, code, summary = resumed.run(resume_run=run_id)
        assert code == 0
        assert summary["observation_object_links"] == 3
        assert FakeEso.calls == 1
        assert FakeSimbad.calls == 1
        assert database.get_run(run_id)["status"] == "completed"

        config_json = json.loads(database.get_run(run_id)["config_json"])
        assert config_json["limit"] == 3
    finally:
        database.close()


def test_partial_alias_stage_is_resumable_and_rebuilds_confidence(tmp_path) -> None:
    FakeEso.calls = FakeSimbad.calls = 0
    FakeSimbad.alias_calls = 0
    FailingAliasSimbad.calls = FailingAliasSimbad.alias_calls = 0
    database = Database(tmp_path / "prototype.sqlite")
    config = RunConfig(
        limit=1,
        simbad_batch_size=10,
        simbad_alias_batch_size=10,
        retries=2,
        output_dir=str(tmp_path / "output"),
    )
    try:
        first = Pipeline(
            database,
            config,
            eso_factory=FakeEso,
            simbad_factory=FailingAliasSimbad,
            sleep=lambda _: None,
            random_source=lambda: 0.0,
        )
        run_id, code, summary = first.run()
        assert code == 2
        assert summary["best_object_incomplete_aliases"] == 1
        assert FailingAliasSimbad.alias_calls == 2

        resumed = Pipeline(
            database,
            config,
            eso_factory=FakeEso,
            simbad_factory=FakeSimbad,
            sleep=lambda _: None,
            random_source=lambda: 0.0,
        )
        _, code, summary = resumed.run(resume_run=run_id)
        assert code == 0
        assert summary["best_object_incomplete_aliases"] == 0
        assert database.uncached_simbad_object_ids_for_run(run_id) == []
        assert FakeSimbad.alias_calls == 1
    finally:
        database.close()
