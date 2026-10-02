"""Read-only local dashboard. No pipeline or catalogue calls are made here."""
from __future__ import annotations

import copy
import csv
import io
import json
import math
import sqlite3
from collections import OrderedDict
from threading import Lock
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from astropy import units as u
from astropy_healpix import HEALPix


REQUIRED = {
    "pipeline_runs": {"run_id", "started_at", "finished_at", "status", "config_json"},
    "observations": {"eso_dp_id", "target_name", "instrument_name", "ra_deg", "dec_deg", "search_radius_deg", "access_url", "healpix_order10"},
    "run_observations": {"run_id", "eso_dp_id"},
    "observation_best_objects": {"run_id", "eso_dp_id", "best_object_name", "broad_category", "subcategory", "classification_detail", "confidence", "match_method", "best_object_key", "separation_arcsec", "alias_complete", "candidate_group_count", "ranking_version", "taxonomy_version"},
    "observation_best_object_members": {"run_id", "eso_dp_id", "catalog", "catalog_object_id", "member_role"},
    "catalog_objects": {"catalog", "catalog_object_id", "preferred_name", "ra_deg", "dec_deg"},
    "observation_objects": {"eso_dp_id", "catalog", "catalog_object_id", "separation_arcsec"},
    "service_calls": {"run_id", "service", "status", "attempt_count", "elapsed_seconds", "error_type", "error_message", "batch_number", "input_count", "result_count", "started_at", "finished_at"},
}
JOIN = """FROM run_observations r JOIN observations o USING (eso_dp_id)
LEFT JOIN observation_best_objects b ON b.run_id=r.run_id AND b.eso_dp_id=r.eso_dp_id"""
FIELDS = """o.eso_dp_id, o.target_name, o.instrument_name, o.ra_deg, o.dec_deg,
o.search_radius_deg, o.access_url, b.best_object_name,
COALESCE(b.broad_category, 'Pending') AS broad_category,
COALESCE(b.confidence, 'pending') AS confidence, b.separation_arcsec,
b.subcategory, b.classification_detail, b.match_method, b.candidate_group_count,
b.alias_complete, b.ranking_version, b.taxonomy_version"""


def age_seconds(stamp: str | None, now: datetime) -> float | None:
    if not stamp:
        return None
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0, (now - parsed).total_seconds())


def compact_moc(cells, order=5):
    """Merge complete nested HEALPix sibling sets into multi-order cells."""
    levels = {order: set(cells)}
    for level in range(order, 0, -1):
        current = levels[level]
        parents = {cell >> 2 for cell in current}
        merged = {parent for parent in parents if all((parent << 2) + i in current for i in range(4))}
        current.difference_update((parent << 2) + i for parent in merged for i in range(4))
        levels[level - 1] = merged
    return {str(level): sorted(cells) for level, cells in sorted(levels.items()) if cells}


def sky_region(params):
    """Conservative cone enclosing the viewport, with indexed HEALPix ranges."""
    keys = ("view_ra", "view_dec", "view_radius")
    if not any(key in params for key in keys):
        return "", [], 180.0
    if not all(key in params for key in keys):
        raise ValueError("Supply view_ra, view_dec and view_radius together")
    ra, dec, radius = (float(params[key]) for key in keys)
    if not all(math.isfinite(v) for v in (ra, dec, radius)) or not -90 <= dec <= 90 or not 0 < radius <= 180:
        raise ValueError("Invalid sky viewport")
    ra %= 360
    if radius >= 90:
        return "", [], radius
    clauses = ["o.dec_deg BETWEEN ? AND ?"]
    values = [max(-90, dec-radius), min(90, dec+radius)]
    if abs(dec) + radius < 90:
        span = math.degrees(math.asin(math.sin(math.radians(radius))/math.cos(math.radians(dec))))
        lo, hi = (ra-span) % 360, (ra+span) % 360
        clauses.append("(o.ra_deg >= ? OR o.ra_deg <= ?)" if lo > hi else "o.ra_deg BETWEEN ? AND ?")
        values.extend((lo, hi))
    # A coarse cone has a small number of ranges, even for a wide viewport.
    order = min(10, max(0, math.ceil(math.log2(120 / radius))))
    cells = sorted(int(cell) for cell in HEALPix(nside=2**order, order="nested").cone_search_lonlat(ra*u.deg, dec*u.deg, radius*u.deg))
    ranges = []
    for cell in cells:
        lo, hi = cell << (2*(10-order)), ((cell+1) << (2*(10-order))) - 1
        if ranges and lo == ranges[-1][1]+1:
            ranges[-1][1] = hi
        else:
            ranges.append([lo, hi])
    clauses.insert(0, "r.eso_dp_id IN (SELECT eso_dp_id FROM observations WHERE " + " OR ".join("healpix_order10 BETWEEN ? AND ?" for _ in ranges) + ")")
    return " AND " + " AND ".join(clauses), [v for pair in ranges for v in pair] + values, radius


class DashboardReader:
    def __init__(self, path: str | Path, expected_interval_hours: float | None = None):
        self.path = Path(path).expanduser().resolve()
        self.expected_interval_hours = expected_interval_hours
        self._cache = OrderedDict()
        self._cache_lock = Lock()

    def signature(self):
        # Commits may change the WAL while the database file stays unchanged.
        result = []
        for path in (self.path, Path(str(self.path) + "-wal")):
            try:
                stat = path.stat()
                result.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns))
            except FileNotFoundError:
                result.append(None)
        return tuple(result)

    def cached(self, key, compute, signature=None):
        signature = self.signature() if signature is None else signature
        with self._cache_lock:
            if (signature, key) in self._cache and signature == self.signature():
                self._cache.move_to_end((signature, key))
                return copy.deepcopy(self._cache[(signature, key)])
        value = compute()
        if signature == self.signature():
            with self._cache_lock:
                self._cache[(signature, key)] = value
                while len(self._cache) > 12:
                    self._cache.popitem(last=False)
        return copy.deepcopy(value)

    @contextmanager
    def connect(self):
        if not self.path.is_file():
            raise ValueError(f"Database not found: {self.path}. Run the pipeline first or supply --database.")
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=3)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            for table, columns in REQUIRED.items():
                actual = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
                if not columns <= actual:
                    raise ValueError(f"Incompatible dashboard schema: {table} is missing required columns. Choose a current SIMBAD-only database; the dashboard will not migrate it.")
            connection.execute("BEGIN")
            yield connection
        finally:
            connection.close()

    @staticmethod
    def run(connection, run_id: str | None):
        if not run_id or run_id == "latest":
            row = connection.execute("SELECT * FROM pipeline_runs ORDER BY started_at DESC, run_id DESC LIMIT 1").fetchone()
        else:
            row = connection.execute("SELECT * FROM pipeline_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise ValueError("The selected run does not exist.")
        return dict(row) if row else None

    def snapshot(self, run_id: str | None = None, now: datetime | None = None):
        now = now or datetime.now(UTC)
        result = self.cached(("snapshot", run_id), lambda: self._snapshot(run_id, now))
        result["refreshed_at"] = now.isoformat()
        health = result["health"]
        latest = health["latest_run"]
        health["age_seconds"] = age_seconds(latest["finished_at"] or latest["started_at"], now) if latest else None
        success_age = age_seconds(health["last_success_at"], now)
        health["overdue"] = self.expected_interval_hours is not None and (success_age is None or success_age > (self.expected_interval_hours + 2) * 3600)
        for row in result["history"]:
            if not row["finished_at"]:
                row["duration_seconds"] = age_seconds(row["started_at"], now)
        return result

    def _snapshot(self, run_id: str | None = None, now: datetime | None = None):
        now = now or datetime.now(UTC)
        with self.connect() as c:
            latest = self.run(c, None)
            selected = self.run(c, run_id)
            history = [dict(row) for row in c.execute("""SELECT p.run_id,p.started_at,p.finished_at,p.status,
                (SELECT COUNT(*) FROM run_observations r WHERE r.run_id=p.run_id) AS observations
                FROM pipeline_runs p ORDER BY started_at DESC, run_id DESC LIMIT 50""")]
            for row in history:
                row["duration_seconds"] = age_seconds(row["started_at"], datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else now)
            last_success = c.execute("SELECT finished_at FROM pipeline_runs WHERE status='completed' AND finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1").fetchone()
            success_age = age_seconds(last_success[0], now) if last_success else None
            overdue = self.expected_interval_hours is not None and (success_age is None or success_age > (self.expected_interval_hours + 2) * 3600)
            calls = [dict(row) for row in c.execute("SELECT * FROM service_calls WHERE run_id=? ORDER BY service,batch_number", (latest["run_id"],))] if latest else []
            health = {
                "latest_run": latest, "calls": calls,
                "retries": sum(max(0, row["attempt_count"] - 1) for row in calls),
                "failed_batches": sum(row["status"] == "failed" for row in calls),
                "age_seconds": age_seconds(latest["finished_at"] or latest["started_at"], now) if latest else None,
                "last_success_at": last_success[0] if last_success else None,
                "expected_interval_hours": self.expected_interval_hours, "overdue": overdue,
            }
            totals = {table: c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("observations", "catalog_objects", "pipeline_runs")}
            result = {"refreshed_at": now.isoformat(), "database": str(self.path), "health": health, "history": history, "totals": totals, "selected_run": selected, "stats": None}
            if selected is None:
                return result
            rid = selected["run_id"]
            count = c.execute(f"""SELECT COUNT(*) AS observations,
                SUM(b.best_object_key IS NOT NULL) AS matches,
                SUM(b.eso_dp_id IS NOT NULL AND b.best_object_key IS NULL) AS unmatched,
                SUM(b.eso_dp_id IS NULL) AS pending {JOIN} WHERE r.run_id=?""", (rid,)).fetchone()
            stats = {key: value or 0 for key, value in dict(count).items()}
            stats["unique_objects"] = c.execute("""SELECT COUNT(*) FROM (SELECT DISTINCT oo.catalog,oo.catalog_object_id
                FROM observation_objects oo JOIN run_observations r USING(eso_dp_id) WHERE r.run_id=?)""", (rid,)).fetchone()[0]
            for key, expression in (("categories", "COALESCE(b.broad_category,'Pending')"), ("confidence", "COALESCE(b.confidence,'pending')"), ("instruments", "COALESCE(o.instrument_name,'Unknown')")):
                stats[key] = [dict(row) for row in c.execute(f"SELECT {expression} AS label,COUNT(*) AS count {JOIN} WHERE r.run_id=? GROUP BY {expression} ORDER BY count DESC,label", (rid,))]
            result["stats"] = stats
            return result

    @staticmethod
    def filters(run_id, params):
        clauses, values = ["r.run_id=?"], [run_id]
        for parameter, expression in (("instrument", "COALESCE(o.instrument_name,'Unknown')"), ("category", "COALESCE(b.broad_category,'Pending')"), ("confidence", "COALESCE(b.confidence,'pending')")):
            if params.get(parameter):
                clauses.append(expression + "=?")
                values.append(params[parameter])
        if params.get("search"):
            # Literal substring search, including names containing % or _.
            term = params["search"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("(o.eso_dp_id LIKE ? ESCAPE '\\' OR o.target_name LIKE ? ESCAPE '\\' OR b.best_object_name LIKE ? ESCAPE '\\')")
            values.extend([f"%{term}%"] * 3)
        return " AND ".join(clauses), values

    def results(self, run_id=None, params=None):
        params = params or {}
        page = max(1, int(params.get("page", 1)))
        size = params.get("page_size", "10")
        if str(size) not in {"10", "50", "100", "1000", "all"}:
            raise ValueError("page_size must be 10, 50, 100, 1000 or all")
        page_size = 1000 if size == "all" else int(size)
        signature = self.signature()
        with self.connect() as c:
            run = self.run(c, run_id)
            if run is None:
                return {"run_id": None, "rows": [], "total": 0, "page": 1, "pages": 1, "page_size": page_size}
            where, values = self.filters(run["run_id"], params)
            total = self.cached(("count", where, tuple(values)), lambda: c.execute(f"SELECT COUNT(*) {JOIN} WHERE {where}", values).fetchone()[0], signature=signature)
            pages = max(1, math.ceil(total / page_size))
            page = min(page, pages)
            query = f"SELECT {FIELDS} {JOIN} WHERE {where} ORDER BY r.eso_dp_id"
            rows = [dict(row) for row in c.execute(query + " LIMIT ? OFFSET ?", [*values, page_size, (page - 1) * page_size])]
            return {"run_id": run["run_id"], "rows": rows, "total": total, "page": page, "pages": pages, "page_size": page_size}

    def export_csv(self, run_id=None, params=None):
        """Yield bounded CSV chunks instead of retaining the full result in memory."""
        with self.connect() as c:
            run = self.run(c, run_id)
            where, values = self.filters(run["run_id"] if run else None, params or {})
            cursor = c.execute(f"SELECT {FIELDS} {JOIN} WHERE {where} ORDER BY r.eso_dp_id", values)
            stream = io.StringIO(newline="")
            writer = csv.writer(stream)
            writer.writerow(column[0] for column in cursor.description)
            yield stream.getvalue().encode("utf-8")
            while rows := cursor.fetchmany(500):
                stream.seek(0)
                stream.truncate(0)
                writer.writerows(rows)
                yield stream.getvalue().encode("utf-8")

    def sky(self, run_id=None, params=None):
        params = params or {}
        relevant = tuple((key, params.get(key, "")) for key in ("search", "instrument", "category", "confidence", "sky_mode", "view_ra", "view_dec", "view_radius"))
        return self.cached(("sky", run_id, relevant), lambda: self._sky(run_id, params))

    def _sky(self, run_id, params):
        mode = params.get("sky_mode", "auto")
        if mode not in {"auto", "points", "moc"}:
            raise ValueError("sky_mode must be auto, points or moc")
        signature = self.signature()
        with self.connect() as c:
            run = self.run(c, run_id)
            if run is None:
                return {"run_id": None, "positions": [], "objects": [], "total_positions": 0, "total_objects": 0, "mode": "points", "coverage": []}
            where, values = self.filters(run["run_id"], params or {})
            region, region_values, radius = sky_region(params)
            where += region
            values.extend(region_values)
            count = self.cached(("count", where, tuple(values)), lambda: c.execute(f"SELECT COUNT(*) {JOIN} WHERE {where}", values).fetchone()[0], signature=signature)
            grouped = f"""SELECT o.ra_deg,o.dec_deg,MIN(o.eso_dp_id) AS eso_dp_id,COUNT(*) AS count,
                CASE WHEN COUNT(DISTINCT COALESCE(b.broad_category,'Pending'))=1
                THEN MIN(COALESCE(b.broad_category,'Pending')) ELSE 'Mixed' END AS category
                {JOIN} WHERE {where} GROUP BY o.ra_deg,o.dec_deg"""
            # Fetch at most one extra position to decide whether markers are safe.
            positions = []
            if mode == "points" or (mode == "auto" and count <= 2000) or radius <= 15:
                limit = 5000 if mode == "points" else 2001
                positions = [dict(row) for row in c.execute(grouped + " ORDER BY o.ra_deg,o.dec_deg LIMIT ?", [*values, limit])]
            coverage_mode = mode != "points" and (len(positions) > 2000 or (count > 0 and not positions))
            if coverage_mode:
                order = min(10, max(5, 5 + int(math.log2(90 / max(radius, 0.01)))))
                # Reduce resolution if necessary, preserving ALL centres while
                # keeping the response bounded to 12,000 occupied category cells.
                while True:
                    shift = 2 * (10-order)
                    query = f"""SELECT COALESCE(b.broad_category,'Pending') AS category,
                        (o.healpix_order10 >> {shift}) AS cell, COUNT(*) AS count
                        {JOIN} WHERE {where} AND o.healpix_order10 >= 0 AND o.healpix_order10 < 12582912
                        GROUP BY category, cell ORDER BY category, cell LIMIT 12001"""
                    cells = c.execute(query, values).fetchall()
                    if len(cells) <= 12000 or order == 0:
                        break
                    order -= 1
                categories = {}
                for row in cells:
                    item = categories.setdefault(row["category"], {"cells": [], "count": 0})
                    item["cells"].append(row["cell"])
                    item["count"] += row["count"]
                coverage = [{"category": label, "count": item["count"], "moc": compact_moc(item["cells"], order)} for label, item in categories.items()]
                return {"run_id": run["run_id"], "mode": "moc", "coverage": coverage,
                        "total_spectra": count, "covered_spectra": sum(item["count"] for item in coverage),
                        "moc_order": order, "positions": [], "objects": [], "total_positions": None, "total_objects": None}
            total = c.execute(f"SELECT COUNT(*) FROM ({grouped})", values).fetchone()[0]
            object_query = f"""SELECT DISTINCT co.catalog,co.catalog_object_id,co.preferred_name,co.ra_deg,co.dec_deg
                {JOIN} JOIN observation_best_object_members m ON m.run_id=r.run_id AND m.eso_dp_id=r.eso_dp_id AND m.member_role='primary'
                JOIN catalog_objects co ON co.catalog=m.catalog AND co.catalog_object_id=m.catalog_object_id
                WHERE {where}"""
            object_total = c.execute(f"SELECT COUNT(*) FROM ({object_query})", values).fetchone()[0]
            objects = [dict(row) for row in c.execute(object_query + " ORDER BY co.catalog,co.catalog_object_id LIMIT 2000", values)]
            return {"run_id": run["run_id"], "positions": positions, "objects": objects, "total_positions": total, "total_objects": object_total, "mode": "points", "coverage": [], "total_spectra": count}

    def detail(self, run_id, product_id):
        with self.connect() as c:
            run = self.run(c, run_id)
            if run is None:
                raise ValueError("The database contains no runs.")
            row = c.execute(f"SELECT {FIELDS} {JOIN} WHERE r.run_id=? AND o.eso_dp_id=?", (run["run_id"], product_id)).fetchone()
            if row is None:
                raise ValueError("Spectrum not found in the selected run.")
            detail = dict(row)
            detail["candidates"] = [dict(r) for r in c.execute("""SELECT co.preferred_name,oo.separation_arcsec
                FROM observation_objects oo JOIN catalog_objects co USING(catalog,catalog_object_id)
                WHERE oo.eso_dp_id=? ORDER BY oo.separation_arcsec LIMIT 50""", (product_id,))]
            same = c.execute("""SELECT o.eso_dp_id FROM observations o JOIN run_observations r USING(eso_dp_id)
                WHERE r.run_id=? AND o.ra_deg=? AND o.dec_deg=? ORDER BY o.eso_dp_id LIMIT 50""", (run["run_id"], row["ra_deg"], row["dec_deg"])).fetchall()
            detail["coincident_products"] = [r[0] for r in same]
            detail["coincident_count"] = c.execute(
                "SELECT COUNT(*) FROM observations o JOIN run_observations r USING(eso_dp_id) WHERE r.run_id=? AND o.ra_deg=? AND o.dec_deg=?",
                (run["run_id"], row["ra_deg"], row["dec_deg"]),
            ).fetchone()[0]
            return detail


def make_server(reader: DashboardReader, port: int = 8765):
    assets = files("eso_object_types").joinpath("dashboard_assets")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            params = {key: values[-1] for key, values in parse_qs(url.query).items()}
            try:
                if url.path in ("/", "/app.js", "/styles.css"):
                    filename, mime = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/styles.css": ("styles.css", "text/css")}[url.path]
                    self.send_body(200, assets.joinpath(filename).read_bytes(), mime)
                    return
                if url.path == "/api/snapshot":
                    data = reader.snapshot(params.get("run"))
                elif url.path == "/api/results":
                    data = reader.results(params.get("run"), params)
                elif url.path == "/api/sky":
                    data = reader.sky(params.get("run"), params)
                elif url.path == "/api/detail":
                    data = reader.detail(params.get("run"), params.get("product", ""))
                elif url.path == "/api/export.csv":
                    chunks = reader.export_csv(params.get("run"), params)
                    # Validate and obtain the header before sending a success response.
                    first = next(chunks)
                    try:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/csv; charset=utf-8")
                        self.send_header("Content-Disposition", 'attachment; filename="eso-spectrum-results.csv"')
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(first)
                        for chunk in chunks:
                            self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    finally:
                        chunks.close()
                    return
                else:
                    self.send_body(404, b'{"error":"Not found"}', "application/json")
                    return
                self.send_body(200, json.dumps(data, allow_nan=False).encode(), "application/json")
            except (ValueError, sqlite3.Error, OSError) as error:
                self.send_body(503 if isinstance(error, (sqlite3.Error, OSError)) or "schema" in str(error) or "Database not found" in str(error) else 400, json.dumps({"error": str(error)}).encode(), "application/json")

        def send_body(self, status, body, mime):
            self.send_response(status)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            if len(args) > 1 and str(args[1]) != "200":
                super().log_message(format, *args)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve_dashboard(path, port=8765, expected_interval_hours=None):
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    if expected_interval_hours is not None and (not math.isfinite(expected_interval_hours) or expected_interval_hours <= 0):
        raise ValueError("expected interval must be a positive finite number")
    reader = DashboardReader(path, expected_interval_hours)
    with make_server(reader, port) as server:
        print(f"Dashboard: http://127.0.0.1:{server.server_port}", flush=True)
        print(f"Read-only database: {reader.path}", flush=True)
        print("Press Ctrl+C to stop. Refreshing this dashboard never runs the pipeline.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0
