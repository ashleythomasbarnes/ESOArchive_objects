from __future__ import annotations

import os

import pytest

from eso_object_types.database import Database
from eso_object_types.models import RunConfig
from eso_object_types.pipeline import Pipeline


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1",
    reason="set RUN_LIVE_TESTS=1 to contact live archive services",
)
def test_live_two_spectrum_smoke(tmp_path) -> None:
    database = Database(tmp_path / "live.sqlite")
    try:
        config = RunConfig(
            limit=2,
            simbad_batch_size=2,
            ned_batch_size=2,
            retries=2,
            output_dir=str(tmp_path / "output"),
        )
        run_id, exit_code, summary = Pipeline(database, config).run()
        assert exit_code == 0, f"live run {run_id} was partial"
        assert summary["observations"] == 2
        assert summary["best_object_rows"] == 2
        assert database.connection.execute(
            """
            SELECT COUNT(*) FROM observation_best_objects
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()[0] == 2
        assert database.uncached_simbad_object_ids_for_run(run_id) == []
        assert database.connection.execute(
            """
            SELECT COUNT(*)
            FROM observation_objects AS oo
            JOIN observations AS o USING (eso_dp_id)
            WHERE oo.separation_arcsec > o.search_radius_deg * 3600.0 + 1e-6
            """
        ).fetchone()[0] == 0
    finally:
        database.close()
