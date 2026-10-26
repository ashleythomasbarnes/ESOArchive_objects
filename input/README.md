# Prepare target coordinates for later object matching

This directory is a standalone preprocessing step. It needs Python 3.11+,
Astropy 6.1+ and NumPy, with no imports from either ESO package and no network
queries. It preserves the supplied CSV and prepares an independent SQLite file.
The main ESO Objects package is **not yet connected** to this database.

For the main pipeline's large-trial launch and recovery instructions, see
[../docs/first_2m_run.md](../docs/first_2m_run.md). Its `--database` option is a
writable enrichment output, so do not point it at this prepared input.

From the repository root, using the existing environment:

```bash
.venv/bin/python input/prepare_target_database.py --self-test
.venv/bin/python input/prepare_target_database.py
```

For a separate environment, install `astropy` and `numpy` with pip. The script
also works from any launch directory when invoked by its absolute path. Its
default input is `targetkeywords_allp3spectra_20261014T162800Z.csv.gz` beside the
script; default output is the same basename with `.sqlite` instead of `.csv.gz`.
Explicit paths and batch size can be supplied:

```bash
python /absolute/path/to/prepare_target_database.py \
  --input /absolute/path/to/table.csv.gz \
  --output /absolute/path/to/table.sqlite --batch-size 10000
```

Plain CSV is also supported. Existing outputs require `--overwrite`. The script
streams batches, preserves duplicate `dp_id` values, and publishes the output
only after completing validation. On failure, its temporary database is removed;
an existing output is preserved. Restart a failed conversion from the beginning.

## Read the corrected coordinates

The `targets` table contains every CSV row and original column, stored as text
(including empty strings, leading zeros and the original numerical formatting).
`row_id` is the one-based CSV record number. `dp_id` is indexed, not unique.

| Added column | Meaning |
| --- | --- |
| `ra_j2000_deg` | Corrected RA in degrees, FK5, equinox **and epoch** J2000; SQL NULL if unusable. |
| `dec_j2000_deg` | Corresponding corrected Dec in degrees; always NULL together with RA. |
| `coordinate_status` | Outcome listed below. |
| `coordinate_notes` | Frame assumptions, skipped steps, tracking flags, approximations, warnings or failure reason. |
| `proper_motion_applied` | 1 only when successful epoch propagation used nonzero proper motion; otherwise 0, including failed conversions. |

**All usable positions are in those same two corrected columns**, including
positions already at J2000, where proper-motion propagation is skipped. Read
this database independently of the enrichment output database, for example:

```sql
SELECT dp_id, ra_j2000_deg, dec_j2000_deg
FROM targets
WHERE ra_j2000_deg IS NOT NULL AND dec_j2000_deg IS NOT NULL;
```

For a read-only Python connection, use
`sqlite3.connect("file:/absolute/path/to/table.sqlite?mode=ro", uri=True)`.

## Conversion rules and limitations

- `targalpha` is packed `HHMMSS.s`; `targdelta` is signed `DDMMSS.s`, not degrees.
  Components are checked, RA 240000 wraps to zero, and the Dec sign is preserved.
- This CSV lacks frame, epoch-system and coordinate-type fields. The agreed
  defaults are **mean FK5 coordinates and Julian years**. This assumption is
  recorded on every row. `--frame FK5|FK4|ICRS` and `--epoch-system J|B` override
  interpretation for the entire input; split heterogeneous inputs if necessary.
  Equinox 1950 alone does not establish FK4. ICRS has no equinox, although this
  script still requires valid epoch/equinox metadata as part of the input contract.
- Epoch (`targepoch`) determines when the position was valid; equinox
  (`targequinox`) describes the coordinate axes. Both must be finite, positive
  years; zero placeholders and missing values are rejected. Output is always
  FK5/equinox J2000 at epoch J2000, not at `mjd_obs`.
- Epoch already J2000: decode and store the coordinates, transforming the frame
  or equinox if necessary. Do not propagate proper motion, even if the supplied
  motions are nonzero, missing or invalid.
- Other epochs require two finite proper-motion components. Confirmed convention:
  `targpma = pmra * cos(delta)`, `targpmd = pmdec`, both in arcseconds/year.
  Valid zero motions require no propagation. Nonzero motions are transformed
  through ICRS, propagated with Astropy `apply_space_motion`, then expressed in FK5.
- Positive finite `targparallax` is interpreted in arcseconds (distance = 1/parallax
  in parsecs); `targradvel` is in km/s. With usable distance, radial velocity
  contributes perspective effects. Missing/invalid RV or absolute values >=99,999
  are treated as unavailable and replaced with zero, with a note. Without usable
  distance, only angular motion is used; RV cannot supply a perspective correction.
  RV/parallax are irrelevant when propagation is skipped. `obj_rv` is preserved,
  but is not substituted for `targradvel`.
- `targtrak=DIFFERENTIAL` is unsupported for stellar-motion propagation and yields
  NULL coordinates. `OFF`, missing and other non-NORMAL tracking values are noted,
  but do not themselves block conversion. No solar-system ephemeris is calculated.
- Missing/invalid target positions never fall back to `ra_pnt`/`de_pnt`. A failure
  affects that row only. These are derived header positions, not independently
  verified catalogue identifications; NULL filtering does not validate assumptions.

| Status | Corrected columns |
| --- | --- |
| `already_j2000` | Populated by decoding; no frame change or motion propagation required. |
| `frame_transformed` | Populated by frame/equinox transformation; input epoch already J2000. |
| `zero_motion` | Populated with valid zero input motions at another epoch; frame transformed if needed. |
| `motion_propagated` | Populated after nonzero proper-motion propagation to J2000. |
| `missing_coordinates` | Both NULL: missing Alpha or Delta. |
| `invalid_coordinates` | Both NULL: malformed, nonfinite or out-of-range packed components. |
| `invalid_metadata` | Both NULL: missing/invalid epoch or equinox. |
| `invalid_proper_motion` | Both NULL: required non-J2000 motion unavailable or invalid. |
| `unsupported_tracking` | Both NULL: differential tracking. |
| `conversion_failed` | Both NULL: transformation failed or produced unusable coordinates. |

The `metadata` table stores JSON-encoded values keyed by name: compressed-source
SHA-256, source path/size, timestamps, conversion version, settings, units,
dependency versions, row count and status counts. The script checks SQLite
integrity, row counts, coordinate ranges, paired NULLs and status consistency.
`--self-test` exercises synthetic conversion cases and a temporary compressed CSV
round trip, duplicate preservation and overwrite refusal; it does not build the
production database.

## Verified local inventory, 2026-10-21

The existing `targetkeywords_allp3spectra_20261014T162800Z.sqlite` contains
2,425,711 rows, with 1,898,880 usable coordinate pairs and 526,831 paired-NULL
rows. Its size is 980,152,320 bytes. Read-only checks returned SQLite integrity
`ok`, zero unpaired NULLs and the following status counts:

| Status | Rows |
| --- | ---: |
| `already_j2000` | 1,872,766 |
| `frame_transformed` | 409 |
| `zero_motion` | 15,623 |
| `motion_propagated` | 10,082 |
| `missing_coordinates` | 518,663 |
| `invalid_coordinates` | 68 |
| `invalid_metadata` | 437 |
| `unsupported_tracking` | 7,663 |

All `dp_id` values happen to be distinct in this file; the preparer intentionally
allows duplicates in future inputs. The compressed CSV SHA-256 was rechecked
against stored metadata:
`266d875c0cbee7e9e9b85917ccd8e7df60ce9dab65c881ca974b4053ca6ced6c`.
The database records conversion version `1`, default FK5/Julian-year settings
and completion on 2026-10-15. The self-test passed again on 2026-10-21; the full
conversion and independent coordinate comparisons were not repeated for this
documentation handoff.

Before future enrichment integration, agree how to join product IDs, handle
duplicates and unusable positions, obtain footprint metadata and transform
FK5/J2000 coordinates into the ICRS frame used by the current SIMBAD query.
Do not treat this table as a drop-in replacement for ObsCore metadata.

References: [Astropy space motion](https://docs.astropy.org/en/stable/coordinates/apply_space_motion.html)
and [ESO target details and motion units](https://www.eso.org/sci/facilities/paranal/instruments/crires/doc/ESO-254264_CRIRES_User_Manual_P109.4.pdf).
