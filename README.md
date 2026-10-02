# ESO archive object-type enrichment prototype

This prototype finds catalogued astronomical objects inside the footprints of
ESO reduced spectra. It queries ESO for spectrum metadata, searches the same
positions in SIMBAD, then stores the object names, object types, and
object-to-spectrum links in SQLite and CSV files.

The workflow uses metadata only. It does not download the spectral FITS files
and does not establish that a catalogued object was detected in a spectrum.

## Quick start

From the repository root, create an environment and install the package:

```bash
conda create -n eso-object-types python=3.12 pip -y
conda activate eso-object-types
python -m pip install -e '.[test,notebook]'
```

Alternatively, use a Python virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test,notebook]'
```

Run the default 50-spectrum test:

```bash
eso-object-types run
```

The command prints a `run_id` and a summary. The main outputs are:

```text
output/
├── eso_object_types.sqlite
├── logs/<run_id>.jsonl
└── <run_id>/
    ├── observations.csv
    ├── catalog_objects.csv
    ├── catalog_object_aliases.csv
    ├── object_types.csv
    ├── observation_objects.csv
    ├── observation_best_objects.csv
    ├── observation_best_object_members.csv
    └── run_summary.csv
```

Start with `output/<run_id>/run_summary.csv` for a compact overview, or open
the interpretation notebook:

```bash
jupyter lab notebooks/interpret_outputs.ipynb
```

## Local dashboard

Start the read-only spectrum monitor from the repository root:

```bash
eso-object-types dashboard
```

Open <http://127.0.0.1:8765>. The single-page dashboard shows the latest run's
health, results and classifications, an Aladin sky viewer, searchable spectrum
results, filtered CSV downloads, and run/batch diagnostics. Light and dark themes
are available. Aladin imagery requires internet access and WebGL; the other
results remain usable if the sky viewer is unavailable.

The table uses SQL filters and pagination, with 10 rows by default. Choose 50,
100 or 1,000 rows, or browse All in bounded batches of 1,000; CSV exports stream
all matching rows. Unchanged summaries, counts and sky layers are cached and
invalidated when the database or WAL changes. Substring searches and deep pages
can still take longer on large databases.

Aladin uses Mellinger colour imagery in stereographic projection. The sky view
queries a padded region around the viewport when you pan or zoom. Coverage uses
lightly filled, category-coloured HEALPix cells, refining from order 5 to order 10
as you zoom. Auto and Coverage switch to clickable markers when a narrow region
contains at most 2,000 distinct positions. Dense regions remain coverage; zoom
further to reveal sources. Explicit Points mode is capped at 5,000 markers.
Spatial queries use the existing HEALPix index, and coverage responses are capped
at 12,000 occupied category cells by reducing resolution when necessary. These
cells contain spectrum centres, rather than observation footprints. Individual
source details appear below the viewer.

The database view refreshes every 30 seconds and follows new manual runs.
Refreshing the dashboard never runs the pipeline or queries ESO/SIMBAD.
Historical runs are selectable, while the health strip always reports the latest
run. Raw catalogue metadata and candidate links reflect the current database;
best classifications are stored per run.

To read another database, supply its path (an absolute path works from any
launch directory), or choose another local port:

```bash
eso-object-types dashboard --database /absolute/path/to/eso_object_types.sqlite --port 8765
```

Manual mode shows data age without overdue warnings. Once a separate scheduler
runs the pipeline daily, enable warnings when no successful run has completed
within 24 hours plus a two-hour grace period:

```bash
eso-object-types dashboard --expected-interval-hours 24
```

This option monitors freshness only; it does not schedule ingestion. Service
health reflects recorded calls, not live availability or worker liveness.
Plots show the selected run, and cumulative database totals are labelled
separately. A future incremental pipeline will need a cumulative results view.
The dashboard never creates or migrates the database. Stop the server with Ctrl+C.

## How the method works

### 1. Select spectra from the ESO archive

The pipeline sends an ADQL query to the public ESO ObsCore TAP service. It
searches the `ivoa.ObsCore` table for rows that satisfy:

```sql
dataproduct_type = 'spectrum'
AND calib_level >= 2
AND s_ra IS NOT NULL
AND s_dec IS NOT NULL
```

The query selects the first `--limit` rows ordered by `dp_id`, so repeated runs
with the same archive contents are deterministic. It retrieves metadata such
as the product identifier, target name, sky position, field of view, spatial
region, instrument, and access URL. It does not retrieve the data products.

For each spectrum, the prototype defines a circular search radius:

```text
max(s_fov / 2, --min-radius-arcsec)
```

If `s_fov` is missing or invalid, the minimum radius is used. Observations with
identical right ascension, declination, and radius are consolidated into one
search position before either catalog is queried. Any matches are linked back
to every ESO product at that position.

### 2. Search SIMBAD

SIMBAD supports a TAP uploaded table. The pipeline uploads a table containing
the search key, right ascension, declination, and cone radius for many ESO
positions at once. One ADQL query then:

- joins `TAP_UPLOAD.targets` to SIMBAD's `basic` table using `CONTAINS`;
- retrieves the stable object identifier, preferred name, coordinates, and
  primary object-type code, stellar spectral type, and galaxy morphology; and
- joins `otypedef` to retrieve the readable type label, description,
  classification path, and candidate flag.

This is why SIMBAD can accept a large batch. For example, 5,000 positions are
rows in one uploaded table, rather than 5,000 separate cone queries. The
prototype defaults to 50,000 positions per SIMBAD batch and validates a maximum
of 200,000. A large input can still return many more object rows, particularly
in crowded fields, so the largest possible batch is not always the most
practical one.

After the positional query, the pipeline retrieves the alternate identifiers
for newly encountered SIMBAD objects. These identifiers are requested in
batches and cached in the database. They allow names such as `M 31` and
`NGC 224` to be recognised as names for the same object without adding one
query per spectrum.

### 3. Choose one best object

All positional matches are retained, but the pipeline also produces one
best-object row for each ESO spectrum. It first compares the ESO target name
with SIMBAD preferred names and alternate identifiers. Recognised trailing
annotations such as `_offset` are removed only to create an additional name
variant; the original target name is preserved.

Each SIMBAD object is a separate candidate, ranked by:

1. an exact match to the original target name;
2. an exact match after removing a recognised trailing annotation;
3. distance from the centre of the search region; and
4. classification specificity, with a deterministic object-key tie-break.

The selected object receives a broad category such as `Star`, `Galaxy`, or
`Supernova`, followed by a more descriptive subcategory when SIMBAD
provides one. When no finer subcategory is available, the subcategory repeats
the broad category (for example, `Supernova` for SIMBAD `SN*`). Stellar spectral
type and galaxy morphology are retained as details. Candidate supernovae
remain labelled `Candidate supernova`.
The complete broad list is `Star`, `Galaxy`, `Supernova`, `Other transient`,
`Nebula or ISM`, `Star cluster or association`, `Galaxy group or cluster`,
`Compact object`, `Solar-system object`, `Other`, and `Unknown`.

Every selected result includes `high`, `medium`, `low`, or `none` confidence.
A low-confidence row is still the highest-ranked candidate; a row with no
catalog candidate is reported as `Unknown` with `none` confidence.

After successful positional searches, observations with no SIMBAD candidate
receive one final lookup of the original ESO target name against SIMBAD
identifiers. Blank and generic names such as `sky` are skipped. These matches
are labelled `target_name_fallback` and always receive `low` confidence,
including when the named object lies outside the positional search region.
Their actual angular separation is retained. Name lookups are batched and
cached for resume, and never replace positional candidates.

This stage uses the ESO and SIMBAD table results. It does not fetch FITS
headers or make a separate SSA query.

### 4. Store objects and spectrum links

The same object may fall inside several ESO spectra, and each spectrum may
contain several objects. The database therefore stores:

- each ESO observation once;
- each catalog object once per source catalog; and
- a many-to-many link for every object and spectrum pairing.

The best-object tables retain the selected SIMBAD record for provenance while
giving users one simple object classification.

Completed batches are recorded using deterministic hashes. Rerunning an
unfinished run skips completed work, retries failed service calls, and avoids
creating duplicate database records.

## ESO and SIMBAD compared

| Service | Role in this pipeline | Table or tables searched | Query method | Default input batch |
|---|---|---|---|---:|
| ESO | Supplies the reduced-spectrum sample and footprint metadata | `ivoa.ObsCore` | One synchronous ADQL query using `SELECT TOP <limit>` | `--limit`, default 50 |
| SIMBAD | Supplies catalog object names, alternate identifiers, and object types | `basic`, `otypedef`, `ident`, and uploaded tables | One spatial join plus cached alias batches | 50,000 positions; 10,000 alias objects |

The ESO query chooses which spectra to process. The SIMBAD positional
query searches around the sky coordinates and radii derived
from the ESO metadata. The ESO target name is compared with those returned
records locally, after the catalog queries.

Use a fresh database for this SIMBAD-only version; previous two-catalog runs
are not supported.

## Run options

A run with every option shown is:

```bash
eso-object-types run \
  --limit 50 \
  --min-radius-arcsec 1 \
  --simbad-batch-size 50000 \
  --simbad-alias-batch-size 10000 \
  --retries 5 \
  --database output/eso_object_types.sqlite \
  --output-dir output \
  --eso-endpoint https://archive.eso.org/tap_obs \
  --simbad-endpoint https://simbad.cds.unistra.fr/simbad/sim-tap
```

| Parameter | Default | Meaning |
|---|---:|---|
| `--limit` | `50` | Number of reduced-spectrum rows requested from ESO ObsCore. This controls the input sample, not the number of catalog matches. |
| `--min-radius-arcsec` | `1` | Minimum cone radius in arcseconds. The actual radius is the larger of this value and half of ESO's `s_fov`. |
| `--simbad-batch-size` | `50000` | Maximum number of consolidated search positions uploaded in one SIMBAD request. Valid values are 1 to 200,000. |
| `--simbad-alias-batch-size` | `10000` | Maximum number of uncached SIMBAD object identifiers uploaded in one alias request. |
| `--retries` | `5` | Maximum attempts for each ESO or SIMBAD service call. Retries use `Retry-After` when supplied, otherwise exponential backoff with jitter. |
| `--database` | `output/eso_object_types.sqlite` | SQLite database path. The database retains all runs and is the source of truth for reports. |
| `--output-dir` | `output` | Parent directory for run-specific CSV exports and JSONL logs. |
| `--resume-run RUN_ID` | none | Resume the named run using its stored configuration. Completed batches are skipped and unfinished or failed batches are attempted again. |
| `--eso-endpoint` | ESO public TAP URL | ESO ObsCore TAP endpoint. |
| `--simbad-endpoint` | SIMBAD TAP URL | SIMBAD TAP endpoint. The current Astroquery adapter requires an HTTPS URL ending in `/simbad/sim-tap`. |

For example, process 5,000 ESO spectra with:

```bash
eso-object-types run --limit 5000
```

Larger runs may take substantial time, especially in crowded fields.
Increase batch sizes cautiously and in accordance with the remote services'
usage policies.

### Resume a partial run

A fully completed run exits with status `0`. A partial run, where one or more
services or batches failed, exits with status `2`. Copy the printed `run_id`
and resume only the unfinished work:

```bash
eso-object-types run --resume-run <run_id>
```

The stored configuration is authoritative when resuming. Other run options
supplied on the resume command do not replace it.

### Recreate reports

Export the CSV files again for the latest database run:

```bash
eso-object-types report
```

Report options are:

| Parameter | Default | Meaning |
|---|---:|---|
| `--database` | `output/eso_object_types.sqlite` | Database from which to read the stored results. |
| `--run-id RUN_ID` | latest run | Run to export. |
| `--output-dir` | `output` | Parent directory for the recreated `output/<run_id>/` CSV directory. |

## Outputs

### `output/eso_object_types.sqlite`

The complete relational database and source of truth. Its main tables are:

| Table | Contents |
|---|---|
| `pipeline_runs` | Run configuration, start and finish times, status, and summary |
| `observations` | One row per ESO data product |
| `run_observations` | ESO products included in each run |
| `object_types` | Normalized type codes and labels, kept separate by catalog |
| `catalog_objects` | Unique SIMBAD objects |
| `catalog_object_aliases` | Cached alternate identifiers for SIMBAD objects |
| `observation_objects` | Many-to-many object-to-spectrum links and separations |
| `observation_best_objects` | One selected object and canonical classification per spectrum and run |
| `observation_best_object_members` | SIMBAD records supporting each selected object |
| `service_calls` | Batch inputs, attempts, timings, status, and failures |

### `output/<run_id>/observations.csv`

One row per ESO spectrum in the run. Important columns include:

- `eso_dp_id`: ESO data-product identifier;
- `target_name`: target name recorded on the ESO product;
- `ra_deg`, `dec_deg`: position used for the catalog search;
- `s_fov_deg`, `s_region`: spatial metadata returned by ESO;
- `search_radius_deg`: circular radius actually used by the prototype;
- `instrument_name`: ESO instrument;
- `access_url`: product access URL; and
- `healpix_order10`: order-10 nested HEALPix index stored for future scaling.

Use this file to see which spectra were processed and which region was
searched for each one.

### `output/<run_id>/catalog_objects.csv`

One row per unique catalog object linked to an observation in this run.
Important columns are the source `catalog`, `catalog_object_id`,
`preferred_name`, coordinates, and `primary_type_code`. Rows also include
spectral type and galaxy morphology when available.

Use this file to inspect the objects returned by SIMBAD.

### `output/<run_id>/object_types.csv`

One row per distinct primary type used by the matched objects. SIMBAD provides
a type code, readable label, and description.

Use this file to decode the classifications in `catalog_objects.csv`.

### `output/<run_id>/catalog_object_aliases.csv`

The cached alternate SIMBAD identifiers used for target-name matching. This file makes naming conversions such as
Messier-to-NGC matches auditable.

### `output/<run_id>/observation_objects.csv`

The bridge between ESO spectra and catalog objects. Each row records
`eso_dp_id`, `catalog`, `catalog_object_id`, angular `separation_arcsec`, and
the runs that first created and most recently confirmed the link.

For a human-readable joined result:

- join `observations.csv` on `eso_dp_id`; and
- join `catalog_objects.csv` on both `catalog` and `catalog_object_id`.

### `output/<run_id>/observation_best_objects.csv`

One final result per ESO spectrum. Important columns include the selected
object name and key, broad category, subcategory, classification detail,
confidence, name-matching method, normalized separation, candidate count,
supporting catalogs, and raw catalog types.

The broad category is intentionally simple. More specific information remains
in `subcategory`, `classification_detail`, and the raw catalog outputs.

### `output/<run_id>/observation_best_object_members.csv`

The SIMBAD records supporting the selected object. Join this file to
`catalog_objects.csv` to inspect the original names, positions, and types used
to create the selected result.

### `output/<run_id>/run_summary.csv`

A two-column summary of observation counts, unique search positions, matched
and unmatched observations, catalog objects, links, service calls, timings,
counts by raw and best-object type, confidence counts,
alias-cache metrics, and an extrapolation to two million spectra.

### `output/logs/<run_id>.jsonl`

A structured audit log containing the run configuration, batch identifiers,
attempts, timings, row counts, retries, failures, and final status. Use this
for troubleshooting and resume checks.

## Interpreting a match

An object-to-spectrum link means that the catalog position falls inside the
circular search region used for that ESO spectrum. It does not establish that:

- the object was detected in the spectrum;
- the object was the observer's intended target;
- the object contributes significant flux;
- the catalog classification is complete or current; or
- nearby SIMBAD entries are different physical sources.

The current `max(s_fov / 2, minimum radius)` cone is a simplified prototype
footprint. A production system should validate `s_fov` by instrument and
product type and use the full ESO `s_region` geometry where appropriate.

A spectrum with no SIMBAD match is a valid result. It may reflect a
small footprint, catalog scope or coverage, or coordinate differences.

The best-object result is a reproducible ranking of catalog metadata, not proof
that the selected object was detected in the spectrum. The complete raw match
list remains available for checking and later reprocessing.

## Explore and validate

The notebook selects the latest run and shows service summaries, the one-row
best-object results, category and confidence counts, matched names and raw
types, archive-style filtering, unmatched spectra, integrity checks, and the
two-million-spectrum estimate:

```bash
jupyter lab notebooks/interpret_outputs.ipynb
```

Run the offline test suite, which uses mock services:

```bash
python -m pytest
```

Run the optional two-spectrum live test against ESO and SIMBAD:

```bash
RUN_LIVE_TESTS=1 python -m pytest -m live -v
```

The proposed production architecture, archive interface, operational plan,
scientific validation, and open decisions have been moved to
[docs/eso_archive_feature_plan.md](docs/eso_archive_feature_plan.md).
