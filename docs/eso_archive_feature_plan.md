# ESO archive feature implementation plan

This document separates the proposed production implementation from the
prototype run instructions in the [README](../README.md).

## How this could become an ESO archive feature

The prototype separates the problem into components that can be implemented
and scaled independently.

### 1. Define the archive products and footprints

- Start with reduced one-dimensional spectra.
- Confirm how `s_fov` and `s_region` should be interpreted for each instrument.
- Extend later to images, cubes, or other products once their footprint rules
  are agreed.

### 2. Build a production object catalog

- Use SIMBAD's uploaded-table capability for large positional batches.
- Do not send millions of independent queries.
- For NED, obtain a versioned bulk snapshot or an explicitly approved bulk
  workflow.
- Preserve catalog identifiers, source catalog, retrieval date, and catalog
  version.

NED's public TAP service does not advertise uploaded coordinate tables. The
prototype's bounded cone batching is appropriate for testing, but not for an
uncoordinated two-million-spectrum production run.

### 3. Perform the spatial matching near the archive database

- Deduplicate repeated ESO positions before matching.
- Partition the sky using HEALPix or the archive's preferred spatial index.
- Match ESO footprints against a locally staged external-object catalog.
- Store the unique objects and many-to-many observation links.
- Make ingestion idempotent so rerunning a batch does not create duplicates.

### 4. Add object type to archive discovery

The archive interface or API could expose:

- an object-type search field;
- catalog selection, such as SIMBAD, NED, or both;
- an option to show all objects inside the footprint;
- object names and types in the result details; and
- links back to the source catalog for provenance.

The database query would select an object type, follow its catalog objects,
follow the object-to-observation links, and return the matching ESO products.

### 5. Operate and refresh the enrichment

- Record the external catalog version used for every enrichment run.
- Schedule incremental refreshes for new ESO products.
- Define how reclassified, merged, moved, or removed catalog objects are
  handled.
- Monitor matching rates, unmatched products, service failures, runtime, data
  growth, and dense-sky performance.
- Keep failed work resumable and completed batches immutable.

### 6. Validate scientifically before release

- Compare cone matching with real instrument apertures and `s_region`.
- Test sparse and crowded fields separately.
- Review false matches and missed matches with archive scientists.
- Decide whether the interface should distinguish objects in the footprint
  from likely primary targets or likely detections.

## Decisions for a broader implementation

The main decisions are organizational and scientific rather than purely
technical:

1. Which ESO data-product types should be included first?
2. What spatial footprint is authoritative for each product and instrument?
3. Which external catalogs and object-type vocabularies should be exposed?
4. Can NED provide or approve a bulk-access route?
5. How often should external classifications be refreshed?
6. Should the archive show all positional matches, likely main targets, or
   both?
7. Which team owns ongoing catalog ingestion, quality control, and monitoring?

## Suggested meeting walkthrough

For a short manager demonstration:

1. Introduce the archive question: how can a user find ESO spectra whose
   footprint contains a supernova, galaxy, quasar, stellar object, or another
   catalogued object type?
2. Explain that the prototype links ESO observations to independently
   maintained SIMBAD and NED classifications.
3. Show `run_summary.csv` from the latest test run.
4. Open the notebook's object-type counts.
5. Change the example filter to `simbad:SN*` and show the returned ESO spectra.
6. Show the human-readable object list for each spectrum.
7. Finish with the production steps and open decisions in this document.

See [scaling.md](scaling.md) for more detail on service limits, the NED bulk
access requirement, and a proposed production architecture.
