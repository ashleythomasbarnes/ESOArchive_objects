# Scaling beyond the prototype

The next step is an initial two-million-spectrum proof of concept. Use the
[launch and recovery guide](first_2m_run.md) for commands and the dated local
baseline. This document separates existing behaviour from future work.

## What already scales

- Observations, catalog objects, object types, and links have stable primary
  keys and idempotent upserts.
- Repeated ESO coordinate/radius combinations are consolidated, using keys
  formatted to 12 decimal places, and queried once per positional pass.
- SIMBAD receives uploaded coordinate tables instead of one query per
  spectrum. This implementation defaults to 50,000 positions and validates an
  input batch cap of 200,000. This is not a guarantee of remote-service capacity.
- SIMBAD aliases are requested once per new catalog object in cached batches
  of 10,000. Best-object ranking and later report rebuilds use the local
  database and do not fetch FITS headers or submit SSA queries.
- Every service batch has a deterministic hash and committed completion state,
  making interrupted work resumable.
- Order-10 nested HEALPix identifiers are stored with ESO observations for
  later partitioning.

## Limits to measure in the first large run

| Area | Current implementation and consequence |
| --- | --- |
| ESO retrieval | One synchronous `TAPService.search` with `SELECT TOP`, no explicit `MAXREC`, no pagination. The full result is materialized before persistence. Row count must equal `--limit`; truncation is not accepted as success. |
| SIMBAD | Spatial and name queries use async jobs above 1,000 uploaded targets; aliases above 10,000 objects. Queries request the advertised hard result limit. Crowded cones or many aliases can still exceed result limits or create large payloads; inspect TAP warnings/status. |
| Local geometry | Each ESO row is validated and assigned HEALPix in Python. Deduplication retains observation membership and the full input list. |
| Memory | Ranking loads observations and candidates with `fetchall()` and collects aliases/best rows. CSV export also uses `fetchall()` per output. Service batching alone does not bound total RAM. |
| Storage | SQLite retains runs; shared objects/links and aliases grow with catalogue density. WAL, CSVs and logs need space beyond the main DB. Dropbox can add I/O contention. |
| Resume | Saved configuration is authoritative. New batch flags on resume are ignored; changing partition/query semantics needs a deliberate recovery design. |
| Progress | ETA describes each stage and newly processed batches. ESO retrieval and local ranking/export have no fine-grained progress. Recorded service seconds include client-side work, not just server execution. |
| Scientific scope | Cones use ObsCore positions and `s_fov/2`, not instrument-specific `s_region` geometry. Corrected header-target coordinates under `input/` are not integrated. |

Keep the first trial sequential and retain the default batch sizes initially.
The SIMBAD adapter has no explicit overflow-status guard of its own; completed
batch state alone does not establish that a response was untruncated.
Measure service wait/transfer/parsing, local conversion, database writes, ranking
and export separately before changing the design. Investigate explicit result
completeness checks and bounded ingestion, then chunked ranking/streamed exports
if measurements show a need. Async ESO retrieval may improve robustness; do not
assume it is faster. Add concurrency only when evidence and service limits support it.

The old 20,000-spectrum run used 4,267 unique positions and a 36.1 MB database.
Linear extrapolation gives roughly 3.6 GB at two million spectra; 3–5 GB is a
rough planning range, not a bound. Start with at least 10 GB free for enrichment
output, monitor growth and account separately for the prepared input database.
More retained runs, candidate links or aliases can change these ratios.

## Future production architecture, not implemented

- Evaluate PostgreSQL with spatial or Q3C/pgSphere indexing when SQLite no longer
  fits measured requirements.
- Stage ESO metadata and external catalogs in immutable, versioned batches.
- Partition workers by HEALPix range. Keep database upserts idempotent.
- Store catalog release or retrieval timestamps on every object and link run.
- Establish refresh rules for moved, merged, removed, and reclassified catalog
  objects.
- Monitor query duration, response bytes, objects per cone, zero-match rate,
  retries, rate limits, link growth, and database storage.
- Test dense Galactic fields separately from sparse high-latitude fields.
- Validate whether a one-dimensional product's `s_fov` is the scientifically
  correct footprint. Where available, migrate from cones to `s_region`.

Each prototype `run_summary.csv` estimates the number of unique positions and
the number of SIMBAD batches implied by two million spectra. This extrapolation
is a batch-count estimate, not a measured production runtime.
It estimates positional batches only, excluding extra name and alias requests.
