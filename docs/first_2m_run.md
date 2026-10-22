# First two-million-spectrum proof of concept

This describes the implementation checked on **2026-10-21**. Read
[AGENTS.md](../AGENTS.md) before fixing code. The trial is an initial scaling
experiment, not a measured runtime guarantee or a production pipeline.

## Choose the intended input

The implemented enrichment route is **live ESO ObsCore -> SIMBAD -> SQLite and
CSVs**. The command below requests the first 2,000,000 qualifying ObsCore rows
ordered by `dp_id`, using ObsCore positions and cone sizes.

The separate `input/targetkeywords_allp3spectra_20261014T162800Z.sqlite` contains
header-derived target coordinates. There is no CLI option to enrich from that
file. If the trial must use those corrected target positions, local input
integration is still required before launch. Do not pass that file as
`--database`: that flag opens a writable enrichment output database.

Integration must handle FK5/J2000 -> ICRS, paired-NULL filtering, duplicate
`dp_id` rows, the product join and the source of footprint/radius metadata.
Keep input read-only and output separate. See [input/README.md](../input/README.md).

## Checked local baseline

These describe existing files, not results of a new live run:

| Item | Verified value on 2026-10-21 |
| --- | --- |
| Branch | `make_teltargkeywords_table` |
| Imported package | This checkout's `src/eso_object_types/__init__.py`, using `.venv/bin/python`. |
| Offline validation | 71 passed, 1 live test deselected; preparer self-test passed. |
| Existing enrichment DB | `output/eso_object_types.sqlite`, 36,106,240 bytes, schema version 3; best-object columns match current code. |
| Latest stored run | `20261002T145004Z-e61a0f`, completed, 20,000 spectra, 4,267 unique search positions. |
| Stored results for that run | 20,000 best-object rows, 17,923 linked spectra, 2,077 with no match, 28,718 links, 3,195 catalogue objects. |
| Prepared target DB | 2,425,711 rows, 1,898,880 usable coordinate pairs, 526,831 paired-NULL rows, 10,082 propagated rows. |
| Prepared-input checks | SQLite integrity `ok`, no unpaired NULLs; CSV checksum matches stored provenance. |

The stored summary projects 426,700 unique positions and 9 positional SIMBAD
batches at two million spectra. This extrapolates a small sample's deduplication
ratio, excludes extra name/alias batches and is not a runtime prediction.
No two-million-spectrum enrichment or new live-service test was run for this
handoff. These two input workflows need not select the same products.

At handoff, `input/` was untracked and `.DS_Store` had an unrelated modification.
Before moving to another checkout or machine, include the preparer and its
README and transfer the CSV/SQLite separately if needed. Existing `output/`
files are ignored by Git. Do not assume a clone contains either dataset or
commit large generated databases as part of a documentation change.

## Before launch

From the repository root:

```bash
.venv/bin/python -c 'import sys, eso_object_types; print(sys.executable); print(eso_object_types.__file__)'
.venv/bin/python -m pytest -q -m 'not live'
.venv/bin/python input/prepare_target_database.py --self-test
```

The self-test uses synthetic data and a temporary database; it does not rebuild
the 2.4-million-row input. A new environment needs Python 3.11+ and
`python -m pip install -e '.[test,notebook]'`. Optional live-service smoke test:

```bash
RUN_LIVE_TESTS=1 .venv/bin/python -m pytest -m live -v
```

Use local non-synced storage, keep the machine awake or use a persistent terminal,
and check free space. Allow at least 10 GB initially for enrichment database,
WAL, CSVs and logs, then monitor growth. This is headroom from the small run,
not a storage bound. The prepared input is another 980,152,320 bytes if copied.

## Launch the implemented ObsCore trial

Use an absolute output directory outside Dropbox and a fresh database for this
experiment. Choose another directory/name if this example already contains a run.

```bash
ESO_OBJECTS_POC_DIR="$HOME/eso-objects-poc-2m"
mkdir -p "$ESO_OBJECTS_POC_DIR"
.venv/bin/python -u -m eso_object_types run \
  --limit 2000000 \
  --min-radius-arcsec 1 \
  --simbad-batch-size 50000 \
  --simbad-alias-batch-size 10000 \
  --retries 5 \
  --database "$ESO_OBJECTS_POC_DIR/enrichment.sqlite" \
  --output-dir "$ESO_OBJECTS_POC_DIR" \
  > "$ESO_OBJECTS_POC_DIR/console-first-launch.log" 2>&1
```

The process stays in the foreground and writes console output to that file.
In a second terminal, set `ESO_OBJECTS_POC_DIR` to the same absolute directory:

```bash
tail -f "$ESO_OBJECTS_POC_DIR/console-first-launch.log"
```

The initial `Starting ESO object-type run ...` line contains the run ID, even
if the process dies before the final `run_id:` print. Structured events append
to `$ESO_OBJECTS_POC_DIR/logs/RUN_ID.jsonl`. CSVs go under
`$ESO_OBJECTS_POC_DIR/RUN_ID/`.

ESO retrieval is one synchronous request with no per-page progress. SIMBAD
stages report batch progress, elapsed time and stage ETA. The ETA starts after
a new batch and excludes cached work from its timing sample. It does not
estimate the whole pipeline. Check process activity and logs during quiet stages.

Optional read-only monitor, in another terminal from this repository:

```bash
.venv/bin/python -m eso_object_types dashboard \
  --database "$ESO_OBJECTS_POC_DIR/enrichment.sqlite" --port 8765
```

Open [the local dashboard](http://127.0.0.1:8765). It refreshes recorded state;
it does not test live service health, prove a worker is alive, or launch work.

## Diagnose and recover

Preserve the traceback/console log, run ID, exact command, Python/package path,
database path and available disk/RAM. Check that the original writer has stopped
before resuming or rebuilding reports. Avoid two writers for the same run.
Keep the database and `-wal`/`-shm` sidecars together; do not delete them to clear
a lock. Use SQLite backup for a consistent snapshot if the database is open.

Inspect the **enrichment** database using the read-only SQLite CLI. These queries
show the latest run and its calls; substitute an explicit run ID for an older
run. Do not use the package's writable `Database` constructor for inspection.

```bash
sqlite3 -readonly "$ESO_OBJECTS_POC_DIR/enrichment.sqlite" <<'SQL'
.headers on
.mode column
PRAGMA query_only=ON;
PRAGMA user_version;
SELECT run_id, status, started_at, finished_at
FROM pipeline_runs ORDER BY started_at DESC LIMIT 5;
SELECT config_json FROM pipeline_runs ORDER BY started_at DESC LIMIT 1;
SELECT service, status, COUNT(*) AS batches, SUM(attempt_count) AS attempts,
       ROUND(SUM(elapsed_seconds), 1) AS seconds
FROM service_calls
WHERE run_id = (SELECT run_id FROM pipeline_runs ORDER BY started_at DESC LIMIT 1)
GROUP BY service, status;
SELECT service, batch_number, status, attempt_count, error_type, error_message
FROM service_calls
WHERE run_id = (SELECT run_id FROM pipeline_runs ORDER BY started_at DESC LIMIT 1)
  AND status != 'completed'
ORDER BY service, batch_number;
SELECT summary_json FROM pipeline_runs ORDER BY started_at DESC LIMIT 1;
SQL
```

If the CLI is unavailable, use Python `sqlite3.connect(path.as_uri() + '?mode=ro',
uri=True)` with an absolute `pathlib.Path`, then `PRAGMA query_only=ON`.

### Resume without changing the experiment

```bash
ESO_OBJECTS_RUN_ID='replace-with-the-recorded-run-id'
.venv/bin/python -u -m eso_object_types run \
  --database "$ESO_OBJECTS_POC_DIR/enrichment.sqlite" \
  --resume-run "$ESO_OBJECTS_RUN_ID" \
  >> "$ESO_OBJECTS_POC_DIR/console-resume.log" 2>&1
```

Stored `config_json` controls limit, radius, batch sizes, endpoints, retries and
output directory. Supplying new flags alongside `--resume-run` does not change
them. Absolute paths above remain stable; older runs with relative paths should
be resumed from their original working directory.

Completion markers are scoped to a run. Resume skips successful batches and
retries unfinished/failed work, then rebuilds best objects and exports. A new
run without `--resume-run` starts another ESO retrieval and catalogue pass.
Changing batch sizes or query semantics needs a fresh experiment or a deliberate
recovery design, not manual edits to completion records.

### Locate the failure

| Symptom | Check and next action |
| --- | --- |
| ESO returned fewer rows than requested | Inspect `eso_failed`, warnings and errors. The code demands exactly `--limit`; synchronous retrieval has no explicit `MAXREC` or pagination. Distinguish truncation, timeout and insufficient eligible data. Investigate explicit limits/async or staged ingestion in `services.py` and `_fetch_eso`, without accepting a silently incomplete sample. |
| SIMBAD timeout, rate limit or overflow | Find `simbad`, `simbad_name` or `simbad_alias` and the batch/error. Retries use numeric `Retry-After` or exponential backoff with jitter. Resume transient failures. Repeated payload/limit failures need TAP status inspection and possibly smaller batches in a fresh trial. Requesting the advertised result hard limit does not guarantee all matches fit. |
| No name-fallback events | Fallback runs only after all positional batches succeed. Resolve positional failures first. Empty/generic names are intentionally skipped. |
| Alias failures or changed confidence | Positional results may already be saved. Resume aliases and rebuild ranking; inspect `best_object_incomplete_aliases`. Incomplete confidence is not a final result. |
| `running` status with no remaining process | Interruption or an unexpected local exception can bypass final status. Preserve traceback, inspect batch states, then resume the same run; no manual status reset is required. |
| `completed` status but missing/truncated CSVs | Status is committed before export. Resolve disk, permission or memory failure, then use `report` below. Status alone does not validate exports. |
| High RAM or killed process during local work | Observations, targets and candidates are materialized; ranking/export use `fetchall()`. Locate the last event/traceback. Investigate chunking/streaming; network concurrency will not bound RAM. |
| Locked database or slow writes | Check for competing pipeline/report writers, actual path, disk space and sync activity. Prefer a local non-synced trial directory. Preserve WAL files. |
| Incompatible best-object schema | Preserve the old file and use a fresh enrichment DB. Old NED-enabled results are intentionally unsupported. Do not patch around old `classification_conflict` requirements by mutating the results. |
| Many Unknown/no-match rows | Distinguish no candidate from an unclassified candidate. Inspect service completion, match method, footprint and coordinate provenance. Name fallback can legitimately lie outside the cone. |

`partial` normally exits 2; successful completion exits 0. CLI validation/I/O
errors can also exit 2. Unexpected exceptions/interruption need the traceback,
not just the exit code. A partial export is diagnostic, not a finished catalogue.

### Rebuild local reports

After stopping the pipeline writer:

```bash
.venv/bin/python -m eso_object_types report \
  --database "$ESO_OBJECTS_POC_DIR/enrichment.sqlite" \
  --run-id "$ESO_OBJECTS_RUN_ID" \
  --output-dir "$ESO_OBJECTS_POC_DIR"
```

This makes no service queries but writes rebuilt best-object tables and CSVs.
It uses current ranking/taxonomy and cached catalogue data. It does not repair
missing service batches or mark a partial run completed. Raw objects and links
are shared across runs; rebuilding an old run does not reproduce an immutable
historical catalogue snapshot.

## Accept the proof of concept

Check final exit status, the `run_finish` event, completed service calls, requested
observation count and one best-object row per spectrum. Inspect all eight CSVs
listed in the [README](../README.md), especially `run_summary.csv`, and verify
no incomplete aliases remain. Record wall time, stage timings, peak RAM, final
database/WAL/export sizes and TAP warnings. The SIMBAD adapter has no explicit
overflow-status guard of its own; do not accept a response with truncation or
overflow warnings as complete just because a batch says `completed`.
Run read-only `PRAGMA integrity_check`
after the writer stops; this may take time on a large file.

Review crowded/sparse fields, high/low-confidence matches and unmatched spectra.
This validates a metadata-matching proof of concept, not source detection or
authoritative instrument footprints. See [scaling.md](scaling.md) for remaining
limits and [eso_archive_feature_plan.md](eso_archive_feature_plan.md) for proposals.
