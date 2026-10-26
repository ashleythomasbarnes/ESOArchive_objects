#!/usr/bin/env python3
"""Stream ESO target-keyword CSV data into a standalone, corrected SQLite table."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
from functools import lru_cache
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import warnings

import astropy
from astropy import units as u
from astropy.coordinates import FK4, FK5, ICRS, SkyCoord
from astropy.time import Time
import numpy as np

VERSION = "1"
DEFAULT_INPUT = Path(__file__).resolve().parent / "targetkeywords_allp3spectra_20261014T162800Z.csv.gz"
J2000 = Time("J2000")
DERIVED = ("row_id", "ra_j2000_deg", "dec_j2000_deg", "coordinate_status",
           "coordinate_notes", "proper_motion_applied")
ASSUMPTION = "Mean coordinates assumed; input frame={frame}, epoch system={system} (not supplied in CSV)."


def number(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("non-finite value")
    return value


def decode(alpha, delta):
    """ESO packed HHMMSS.s / signed DDMMSS.s, never decimal-degree inputs."""
    a, d = number(alpha), number(delta)
    h, remainder = divmod(a, 10000)
    m, s = divmod(remainder, 100)
    degrees, remainder = divmod(abs(d), 10000)
    dm, ds = divmod(remainder, 100)
    if (a < 0 or (h >= 24 and a != 240000) or m >= 60 or s >= 60
            or degrees > 90 or dm >= 60 or ds >= 60
            or (degrees == 90 and (dm != 0 or ds != 0))):
        raise ValueError("invalid packed sexagesimal components")
    return (15 * (h + m / 60 + s / 3600)) % 360, math.copysign(degrees + dm / 60 + ds / 3600, d)


@lru_cache(maxsize=1024)
def year_time(year, system):
    if year <= 0:
        raise ValueError("epoch/equinox must be positive; zero is not a usable year")
    return Time(year, format="jyear" if system == "J" else "byear")


def transform(group, key, frame_name, system):
    """One vectorized transform for compatible epochs and motion models."""
    equinox, epoch, moving, distance = key
    eq, ep = year_time(equinox, system), year_time(epoch, system)
    frame = {"FK5": lambda: FK5(equinox=eq),
             "FK4": lambda: FK4(equinox=eq, obstime=ep),
             "ICRS": ICRS}[frame_name]()
    kwargs = dict(ra=np.array([g[1] for g in group]) * u.deg,
                  dec=np.array([g[2] for g in group]) * u.deg,
                  frame=frame)
    if frame_name != "FK4":
        kwargs["obstime"] = ep
    if moving:
        kwargs.update(pm_ra_cosdec=np.array([g[3] for g in group]) * u.arcsec/u.yr,
                      pm_dec=np.array([g[4] for g in group]) * u.arcsec/u.yr)
        if distance:
            kwargs.update(distance=np.array([1/g[5] for g in group]) * u.pc,
                          radial_velocity=np.array([g[6] for g in group]) * u.km/u.s)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        coords = SkyCoord(**kwargs)
        if moving:
            coords = coords.transform_to(ICRS()).apply_space_motion(new_obstime=J2000)
        coords = coords.transform_to(FK5(equinox=J2000))
    notes = sorted({str(w.message) for w in caught})
    return coords.ra.deg, coords.dec.deg, notes


def convert_batch(rows, frame="FK5", system="J"):
    results, groups = [], defaultdict(list)
    base_note = ASSUMPTION.format(frame=frame, system=system)
    for i, row in enumerate(rows):
        result = [None, None, "invalid_coordinates", base_note, 0]
        results.append(result)
        tracking = row.get("targtrak", "").strip().upper()
        if tracking != "NORMAL":
            result[3] += f" Tracking={tracking or 'missing'}."
        if tracking == "DIFFERENTIAL":
            result[2] = "unsupported_tracking"
            result[3] += " Differential/solar-system motion is unsupported."
            continue
        if not row["targalpha"].strip() or not row["targdelta"].strip():
            result[2] = "missing_coordinates"
            result[3] += " Missing target Alpha/Delta; pointing coordinates not substituted."
            continue
        try:
            ra, dec = decode(row["targalpha"], row["targdelta"])
        except (ValueError, OverflowError) as error:
            result[3] += f" {error}."
            continue
        try:
            epoch = number(row["targepoch"])
            equinox = number(row["targequinox"])
            ep = year_time(epoch, system)
            year_time(equinox, system)
        except (ValueError, OverflowError) as error:
            result[2] = "invalid_metadata"
            result[3] += f" Missing/invalid epoch or equinox: {error}."
            continue
        already_epoch = abs((ep - J2000).to_value(u.day)) < 1e-9
        if already_epoch and frame == "FK5" and equinox == 2000 and system == "J":
            result[:] = [ra, dec, "already_j2000", result[3] + " Proper motion skipped: epoch already J2000.", 0]
            continue
        pma = pmd = parallax = rv = 0.0
        if not already_epoch:
            try:
                pma, pmd = number(row["targpma"]), number(row["targpmd"])
            except (ValueError, OverflowError) as error:
                result[2] = "invalid_proper_motion"
                result[3] += f" Missing/invalid proper motion needed for epoch propagation: {error}."
                continue
        moving = pma != 0 or pmd != 0
        if moving:
            try:
                parallax = number(row.get("targparallax", ""))
            except (ValueError, OverflowError):
                pass
            parallax = max(parallax, 0)
            try:
                rv = number(row.get("targradvel", ""))
                if abs(rv) >= 99999:
                    raise ValueError("radial-velocity placeholder")
            except (ValueError, OverflowError):
                rv = 0.0
                result[3] += " Radial velocity missing/invalid/placeholder: assumed zero."
            if not parallax:
                result[3] += " No usable positive parallax: angular-only motion; no perspective correction."
        result[2] = "motion_propagated" if moving else "frame_transformed" if already_epoch else "zero_motion"
        if already_epoch:
            result[3] += " Proper motion skipped: epoch already J2000."
        elif not moving:
            result[3] += " Valid zero proper motion: propagation unnecessary."
        key = (equinox, epoch, moving, bool(parallax))
        groups[key].append((i, ra, dec, pma, pmd, parallax, rv))

    def apply(group, key):
        try:
            ra, dec, notes = transform(group, key, frame, system)
            for g, a, d in zip(group, ra, dec):
                result = results[g[0]]
                if not (math.isfinite(a) and math.isfinite(d) and 0 <= a < 360 and -90 <= d <= 90):
                    result[2] = "conversion_failed"
                    result[3] += " Transform returned invalid coordinates."
                    continue
                result[0], result[1], result[4] = float(a), float(d), int(key[2])
                if notes:
                    result[3] += " Astropy: " + " | ".join(notes)
        except Exception as error:
            # Retry individually so a bad row cannot invalidate its batch neighbours.
            if len(group) > 1:
                for g in group:
                    apply([g], key)
            else:
                result = results[group[0][0]]
                result[0], result[1], result[2], result[4] = None, None, "conversion_failed", 0
                result[3] += f" {type(error).__name__}: {error}."
    for key, group in groups.items():
        apply(group, key)
    return results


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate(connection, count):
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("SQLite integrity check failed")
    total, bad = connection.execute('''SELECT COUNT(*), COALESCE(SUM(
        (ra_j2000_deg IS NULL) != (dec_j2000_deg IS NULL)
        OR ra_j2000_deg < 0 OR ra_j2000_deg >= 360
        OR dec_j2000_deg < -90 OR dec_j2000_deg > 90
        OR coordinate_status IS NULL OR coordinate_notes IS NULL
        OR proper_motion_applied NOT IN (0, 1)
        OR ((ra_j2000_deg IS NOT NULL) != (coordinate_status IN
            ('already_j2000','frame_transformed','zero_motion','motion_propagated')))
        OR (proper_motion_applied != (coordinate_status = 'motion_propagated'))
        ), 0) FROM targets''').fetchone()
    if total != count or bad:
        raise ValueError(f"Validation failed: expected={count}, stored={total}, invalid={bad}")
    return dict(connection.execute("SELECT coordinate_status, COUNT(*) FROM targets GROUP BY coordinate_status"))


def build(source, output, batch_size=10000, frame="FK5", system="J", overwrite=False):
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output:
        raise ValueError("Input and output must differ")
    if output.exists() and not overwrite:
        raise FileExistsError(f"Output exists: {output}; use --overwrite to replace it")
    if batch_size < 1:
        raise ValueError("Batch size must be positive")
    initial_stat = source.stat()
    sha256 = checksum(source)
    fd, temporary = tempfile.mkstemp(prefix=output.name + ".", suffix=".tmp", dir=output.parent)
    os.close(fd)
    temporary = Path(temporary)
    connection = None
    count = 0
    started = datetime.now(timezone.utc).isoformat()
    try:
        connection = sqlite3.connect(temporary)
        connection.execute("PRAGMA journal_mode=DELETE")
        opener = gzip.open if source.suffix.lower() == ".gz" else open
        with opener(source, "rt", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            names = reader.fieldnames
            required = {"dp_id", "targalpha", "targdelta", "targepoch", "targequinox", "targpma", "targpmd"}
            if not names or len({n.lower() for n in names}) != len(names) or any(not n for n in names):
                raise ValueError("CSV needs nonempty, unique column names")
            if required - set(names) or {n.lower() for n in names} & set(DERIVED):
                raise ValueError("CSV missing required columns or collides with derived columns")
            raw_columns = ", ".join(quote(n) + " TEXT NOT NULL" for n in names)
            connection.execute(f'''CREATE TABLE targets (row_id INTEGER PRIMARY KEY,
                {raw_columns}, ra_j2000_deg REAL, dec_j2000_deg REAL,
                coordinate_status TEXT NOT NULL, coordinate_notes TEXT NOT NULL,
                proper_motion_applied INTEGER NOT NULL CHECK(proper_motion_applied IN (0,1)),
                CHECK ((ra_j2000_deg IS NULL) = (dec_j2000_deg IS NULL)))''')
            placeholders = ",".join("?" for _ in range(len(names) + 6))
            insert = f"INSERT INTO targets VALUES ({placeholders})"
            batch = []

            def write_batch():
                nonlocal count
                converted = convert_batch(batch, frame, system)
                connection.executemany(insert, ((count+i+1, *[r[n] for n in names], *c)
                                               for i, (r, c) in enumerate(zip(batch, converted))))
                count += len(batch)
                connection.commit()
                if count % 100000 < len(batch):
                    print(f"Imported and converted {count:,} rows", flush=True)
                batch.clear()

            for row in reader:
                if None in row or any(v is None for v in row.values()):
                    raise ValueError(f"Malformed CSV record at line {reader.line_num}")
                batch.append(row)
                if len(batch) >= batch_size:
                    write_batch()
            if batch:
                write_batch()
        final_stat = source.stat()
        if (initial_stat.st_size, initial_stat.st_mtime_ns) != (final_stat.st_size, final_stat.st_mtime_ns):
            raise ValueError("Source changed while reading; output not published")
        print(f"Validating {count:,} rows and building dp_id index...", flush=True)
        connection.execute("CREATE INDEX targets_dp_id ON targets(dp_id)")
        statuses = validate(connection, count)
        metadata = dict(conversion_version=VERSION, source_path=str(source), source_sha256=sha256,
                        source_size_bytes=initial_stat.st_size, started_utc=started,
                        completed_utc=datetime.now(timezone.utc).isoformat(), row_count=count,
                        status_counts=statuses, input_frame=frame, input_epoch_system=system,
                        input_coordinate_type="mean (assumed)", pma_convention="pm_ra_cosdec (confirmed)",
                        proper_motion_unit="arcsec/year", parallax_unit="arcsec", radial_velocity_unit="km/s",
                        output_frame="FK5", output_equinox="J2000", output_epoch="J2000", output_unit="deg",
                        batch_size=batch_size, python_version=sys.version, astropy_version=astropy.__version__,
                        numpy_version=np.__version__, sqlite_version=sqlite3.sqlite_version)
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.executemany("INSERT INTO metadata VALUES (?,?)", ((k, json.dumps(v)) for k,v in metadata.items()))
        connection.commit()
        connection.close()
        connection = None
        if overwrite:
            os.replace(temporary, output)
        else:
            os.link(temporary, output)  # Atomic creation, refusing a concurrent output.
            temporary.unlink()
        print(f"Database: {output}\nRows: {count:,}\n" + json.dumps(statuses, indent=2), flush=True)
        return metadata
    finally:
        if connection is not None:
            connection.close()
        temporary.unlink(missing_ok=True)


def self_test():
    def row(**changes):
        result = dict(dp_id="duplicate", targalpha="120000", targdelta="-300000",
                      targepoch="2000", targequinox="2000", targpma="", targpmd="",
                      targparallax="", targradvel="", targtrak="NORMAL")
        result.update(changes)
        return result

    assert decode("120000", "-300000") == (180, -30)
    assert decode("240000", "900000") == (0, 90)
    assert abs(decode("000001", "-000001")[1] + 1/3600) < 1e-12
    assert math.copysign(1, decode("0", "-0")[1]) == -1
    for a,d in [("126000","0"),("120060","0"),("240001","0"),("-1","0"),("0","900001"),("nan","0")]:
        try:
            decode(a,d)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid packed input accepted")
    fixtures = [row(), row(targpma="9",targpmd="3"), row(targequinox="1950"),
                row(targepoch="1990",targpma="0",targpmd="0"),
                row(targepoch="1990",targpma="1",targpmd="-0.5"),
                row(targepoch="1990"), row(targepoch="0"), row(targalpha=""),
                row(targtrak="DIFFERENTIAL"), row(targepoch="2016",targpma="1",targpmd="2",targparallax="0.1",targradvel="99999")]
    out = convert_batch(fixtures)
    assert [r[2] for r in out] == ["already_j2000","already_j2000","frame_transformed","zero_motion",
                                    "motion_propagated","invalid_proper_motion","invalid_metadata",
                                    "missing_coordinates","unsupported_tracking","motion_propagated"]
    assert out[0][:2] == out[1][:2] == [180, -30]
    assert np.allclose(out[3][:2], [180, -30], atol=1e-12, rtol=0)
    assert all(r[4] == 0 for r in out[:4])
    assert all(r[:2] == [None,None] for r in out[5:9])
    assert "assumed zero" in out[9][3]
    expected = SkyCoord(ra=180*u.deg,dec=-30*u.deg,frame=FK5(equinox=Time("J1950"))).transform_to(FK5(equinox=J2000))
    assert expected.separation(SkyCoord(out[2][0]*u.deg,out[2][1]*u.deg,frame=FK5(equinox=J2000))).arcsec < 1e-7
    # Near-equator simple angular motion independently fixes direction and magnitude.
    motion = convert_batch([row(targdelta="0",targepoch="1990",targpma="1",targpmd="0")])[0]
    assert abs((motion[0]-180)*3600 - 10) < 1e-4
    assert abs(motion[1]*3600) < 1e-4
    assert motion[4] == 1
    # Check full distance/RV propagation against an independent ICRS construction.
    full = convert_batch([row(targepoch="2016", targpma="1", targpmd="-0.5",
                              targparallax="0.1", targradvel="20")])[0]
    initial = SkyCoord(ra=180*u.deg, dec=-30*u.deg, pm_ra_cosdec=1*u.arcsec/u.yr,
                       pm_dec=-0.5*u.arcsec/u.yr, distance=10*u.pc,
                       radial_velocity=20*u.km/u.s, obstime=Time("J2016"),
                       frame=FK5(equinox=J2000))
    expected = initial.icrs.apply_space_motion(new_obstime=J2000).transform_to(FK5(equinox=J2000))
    actual = SkyCoord(full[0]*u.deg, full[1]*u.deg, frame=FK5(equinox=J2000))
    assert actual.separation(expected).arcsec < 1e-7
    mixed = convert_batch([row(targalpha="126000"), row(targequinox="0"),
                           row(targepoch="1990", targpma="nan", targpmd="0"), row()])
    assert [r[2] for r in mixed] == ["invalid_coordinates", "invalid_metadata",
                                     "invalid_proper_motion", "already_j2000"]
    assert all(r[:2] == [None, None] for r in mixed[:3])
    for frame,system in [("FK4","B"),("ICRS","J")]:
        r = convert_batch([row(targepoch="1950",targequinox="1950",targpma="1",targpmd="2")],frame,system)[0]
        assert r[0] is not None and r[4] == 1, r
    with tempfile.TemporaryDirectory() as directory:
        source, output = Path(directory)/"test.csv.gz", Path(directory)/"test.sqlite"
        with gzip.open(source,"wt",newline="") as f:
            writer = csv.DictWriter(f,fieldnames=list(fixtures[0]))
            writer.writeheader()
            writer.writerows(fixtures)
        build(source,output,batch_size=3)
        with sqlite3.connect(output) as conn:
            assert conn.execute("SELECT COUNT(*) FROM targets WHERE dp_id='duplicate'").fetchone()[0] == len(fixtures)
            assert conn.execute("SELECT targpma FROM targets WHERE row_id=1").fetchone()[0] == ""
            validate(conn,len(fixtures))
        try:
            build(source,output)
        except FileExistsError:
            pass
        else:
            raise AssertionError("Existing output overwritten")
    print("All self-tests passed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--batch-size", type=int, default=10000)
    parser.add_argument("--frame", choices=["FK5","FK4","ICRS"], default="FK5")
    parser.add_argument("--epoch-system", choices=["J","B"], default="J")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--self-test", action="store_true", help="Run synthetic checks only")
    args = parser.parse_args()
    try:
        if args.self_test:
            self_test()
        else:
            source = args.input.resolve()
            name = source.name[:-3] if source.suffix.lower() == ".gz" else source.name
            output = args.output or source.with_name(Path(name).stem + ".sqlite")
            build(source,output,args.batch_size,args.frame,args.epoch_system,args.overwrite)
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == "__main__":
    main()
