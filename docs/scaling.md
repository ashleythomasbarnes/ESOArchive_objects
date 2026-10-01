# Scaling beyond the prototype

The 50-spectrum run tests the data model and service interactions. It is not
permission to submit millions of public catalog requests.

## What already scales

- Observations, catalog objects, object types, and links have stable primary
  keys and idempotent upserts.
- Identical ESO coordinate and radius combinations are queried once.
- SIMBAD receives uploaded coordinate tables instead of one query per
  spectrum. Its documented upload limit is 200,000 rows, while this prototype
  defaults to 50,000.
- SIMBAD aliases are requested once per new catalog object in cached batches
  of 10,000. Best-object ranking and later report rebuilds use the local
  database and do not fetch FITS headers or submit SSA queries.
- Every service batch has a deterministic hash and committed completion state,
  making interrupted work resumable.
- Order-10 nested HEALPix identifiers are stored with ESO observations for
  later partitioning.

## Production architecture

- Move the schema to PostgreSQL with spatial or Q3C/pgSphere indexing.
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
