# ESOArchive_objects working guide

Read this file and [README.md](README.md) before changing the pipeline. For the
first large trial and failure recovery, read [docs/first_2m_run.md](docs/first_2m_run.md).
The scope is this repository, not the sibling `ESOArchive_teltargkeywords`
collector. Keep fixes small, preserve existing data, and update the relevant
documentation when behaviour changes.

## Current scope and boundaries

- This is an experimental, **SIMBAD-only**, metadata enrichment pipeline for ESO
  reduced spectra. It does not download spectra or establish a detection.
- `eso-object-types run` obtains its input online from ESO ObsCore. It has no
  local CSV/SQLite input option. `--database` names the enrichment **output**.
- `input/prepare_target_database.py` is an independent, offline CSV-to-SQLite
  preparer. It must remain self-contained, without imports from or changes to
  the enrichment package unless integration is explicitly requested.
- The prepared target database is **not connected to the main pipeline**.
  `--limit 2000000` selects ESO ObsCore rows; it does not read two million rows
  from `input/`. Do not imply these are the same sample or coordinate system.
- Preserve original inputs, existing databases, run directories and logs. Do not
  silently migrate old NED-enabled results, delete failed runs, or rebuild the
  prepared database to diagnose an enrichment failure. The constructor rejects
  incompatible best-object schemas; use a fresh output database.
- Some compatible SIMBAD-only columns are migrated by `Database`; the current
  enrichment schema uses `PRAGMA user_version=3`. The independent input database
  uses its own conversion metadata, not that schema version.

## Setup and validation

Run from the repository root. Prefer the checkout's `.venv/bin/python` and
`python -m eso_object_types` so a different installed package is not used.

```bash
.venv/bin/python -c 'import sys, eso_object_types; print(sys.executable); print(eso_object_types.__file__)'
.venv/bin/python -m pytest -q -m 'not live'
.venv/bin/python input/prepare_target_database.py --self-test
```

If creating a new environment, use Python 3.11+ and install `-e '.[test,notebook]'`.
The opt-in live smoke test is
`RUN_LIVE_TESTS=1 .venv/bin/python -m pytest -m live -v` and contacts both services.
Offline tests and the preparer self-test do not establish current service health
or successful processing of two million spectra. Use temporary databases for
tests; never point fixtures at the user's real output.

## Code and diagnostic map

| File | Responsibility |
| --- | --- |
| `src/eso_object_types/cli.py`, `models.py` | Commands, defaults, configuration validation. |
| `services.py` | ESO/SIMBAD ADQL, TAP adapters, result mapping. |
| `geometry.py` | Coordinate checks, cone radius, position consolidation, hashes, HEALPix. |
| `pipeline.py` | Stage order, retries, cached completion, resume, run status. |
| `database.py` | Schema, transactions, upserts, alias cache, service-call records. |
| `resolution.py`, `taxonomy.py` | Best-object ranking and classification, both currently `v2`. |
| `reports.py` | Summary queries and CSV export. |
| `logging_utils.py` | Appended JSONL audit log, elapsed time and stage ETA. |
| `dashboard.py`, `dashboard_assets/` | Read-only local monitor and browser UI. |
| `notebooks/interpret_outputs.ipynb` | Interactive interpretation of stored results. |
| `input/prepare_target_database.py`, `input/README.md` | Independent coordinate conversion and its scientific assumptions. |

Tests mirror these modules. Pipeline tests cover partial-run resume, alias
failures, name fallback and idempotence. Dashboard tests cover read-only access
and spatial/table queries. Keep notebook source edits focused; avoid output or
metadata churn.

## Enrichment invariants

- ESO input is `ivoa.ObsCore`, `dataproduct_type='spectrum'`, `calib_level>=2`,
  non-NULL `s_ra`/`s_dec`, ordered by `dp_id`, with `TOP <limit>`. Retrieval is one
  synchronous `TAPService.search`, without explicit `MAXREC` or paging. The
  pipeline requires exactly the requested row count before continuing.
- Search positions use ObsCore `s_ra`/`s_dec` as ICRS and a cone radius
  `max(s_fov/2, min_radius_arcsec/3600)` in degrees. Missing/invalid `s_fov`
  uses the minimum. `s_region` is stored but not used for matching. Consolidation
  keys format coordinates and radius to 12 decimal places.
- Order: ESO, SIMBAD positions, target-name fallback if all positional batches
  succeeded, aliases, best-object rebuild, summary/status, CSV exports.
- Fallback is a new online SIMBAD exact-identifier query of the original target
  name for observations without positional matches. Skip blank/generic names.
  Preserve `match_method='target_name_fallback'`, actual separation and **low**
  confidence, including matches outside the cone. Positional candidates win.
- Ranking preserves original-name and recognised base-name matching, normalized
  separation, classification specificity and deterministic tie-breaks. Aliases
  are fetched in batches and cached per object. Preserve incomplete-alias flags.
- Broad types with no finer subtype repeat the category (`SN*` -> `Supernova`);
  candidate supernovae remain `Candidate supernova`. Do not treat `Unknown`
  classification as synonymous with no positional or name match.

## Recovery and scale rules

- Resume with the **same database and run ID**. Stored `config_json` overrides
  supplied run flags, including batch sizes, endpoints and `output_dir`. Run
  from the same directory if saved paths are relative. Do not edit saved hashes,
  configuration or batch status to force a resume after changing batch layout.
- Completed service batches are skipped on resume; a new run does not reuse
  their completion markers. Alias data is cached across runs in a database.
  Raw observations/objects/links are shared across runs, not immutable snapshots.
- Expected service failures produce `partial` and exit 2. Unexpected exceptions
  or interruption can leave `running` records. Check the real process and logs;
  the dashboard has no worker heartbeat. Never run two writers for the same run.
- `finish_run` precedes export. `completed` does not prove every CSV exists.
  `report` rebuilds best-object tables and exports from local data, without
  network queries; **it writes to the database**. Stop the writer before using
  it for recovery. The dashboard is the read-only alternative.
- Diagnose real databases with SQLite `mode=ro` and `PRAGMA query_only=ON`, not
  `Database(...)`, which initializes/migrates tables. Preserve WAL sidecars;
  use SQLite's backup API for a consistent copy if needed.
- Keep the first large proof of concept sequential and start with default
  50,000-position / 10,000-alias batches. Profile before optimizing. ESO row
  limits/timeouts, SIMBAD result overflow, in-memory observations/candidates,
  per-row HEALPix work, `fetchall()` in resolution/export, and Dropbox I/O are
  known investigation points, not established causes of a future failure.
  The SIMBAD adapter has no explicit overflow-status guard of its own; a
  completed batch does not by itself prove the response was untruncated.
- Prefer a local, non-synced output directory. The historical 20,000-row database
  was 36,106,240 bytes; roughly 3.6 GB at 2m is only linear extrapolation. Budget
  at least 10 GB for enrichment DB, WAL, exports and logs, then monitor growth.

## Prepared-coordinate contract

See `input/README.md` for full rules and a dated inventory. All usable positions
are in `ra_j2000_deg` and `dec_j2000_deg`; unusable rows have paired SQL NULLs.
Output is **FK5, equinox J2000, epoch J2000**, not automatically ICRS. Raw ESO
target Alpha/Delta are packed sexagesimal, not degrees. Default interpretation
is mean FK5 and Julian years because the CSV lacks explicit frame/system fields.
`targpma` is `pm_ra_cosdec` in arcsec/year. Already-J2000 epochs skip motion;
`DIFFERENTIAL` tracking is unsupported. Never substitute pointing coordinates.
Keep status, notes and `proper_motion_applied`, preserve raw fields and duplicate
`dp_id` rows, and identify records with `row_id`. A future integration needs an
explicit duplicate policy, footprint metadata and FK5-to-ICRS handling, rather
than silently wiring these values into the current positional query.
