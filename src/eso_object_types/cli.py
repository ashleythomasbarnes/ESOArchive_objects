from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .database import Database
from .models import RunConfig
from .pipeline import Pipeline
from .reports import export_run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eso-object-types",
        description="Enrich ESO reduced spectra with SIMBAD and NED object types.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run or resume enrichment")
    run.add_argument("--limit", type=int, default=50)
    run.add_argument("--min-radius-arcsec", type=float, default=1.0)
    run.add_argument("--simbad-batch-size", type=int, default=50_000)
    run.add_argument("--ned-batch-size", type=int, default=50)
    run.add_argument("--retries", type=int, default=5)
    run.add_argument("--database", default="output/eso_object_types.sqlite")
    run.add_argument("--output-dir", default="output")
    run.add_argument("--resume-run")
    run.add_argument(
        "--eso-endpoint", default="https://archive.eso.org/tap_obs"
    )
    run.add_argument(
        "--simbad-endpoint",
        default="https://simbad.cds.unistra.fr/simbad/sim-tap",
    )
    run.add_argument(
        "--ned-endpoint", default="https://ned.ipac.caltech.edu/tap"
    )

    report = subparsers.add_parser(
        "report", help="Re-export CSV files for an existing run"
    )
    report.add_argument("--database", default="output/eso_object_types.sqlite")
    report.add_argument("--run-id")
    report.add_argument("--output-dir", default="output")
    return parser


def run_command(args: argparse.Namespace) -> int:
    config = RunConfig(
        limit=args.limit,
        min_radius_arcsec=args.min_radius_arcsec,
        simbad_batch_size=args.simbad_batch_size,
        ned_batch_size=args.ned_batch_size,
        retries=args.retries,
        eso_endpoint=args.eso_endpoint,
        simbad_endpoint=args.simbad_endpoint,
        ned_endpoint=args.ned_endpoint,
        output_dir=args.output_dir,
    )
    database = Database(args.database)
    try:
        run_id, exit_code, summary = Pipeline(database, config).run(
            resume_run=args.resume_run
        )
        print(f"run_id: {run_id}")
        print(json.dumps(summary, indent=2, sort_keys=True))
        return exit_code
    finally:
        database.close()


def report_command(args: argparse.Namespace) -> int:
    database = Database(args.database)
    try:
        run_id = args.run_id or database.latest_run_id()
        if run_id is None:
            raise ValueError("the database contains no runs")
        row = database.get_run(run_id)
        if row is None:
            raise ValueError(f"run {run_id!r} does not exist")
        config = RunConfig.from_dict(json.loads(row["config_json"]))
        path, summary = export_run(
            database.connection,
            run_id,
            args.output_dir,
            config.simbad_batch_size,
            config.ned_batch_size,
        )
        print(f"exports: {path}")
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    finally:
        database.close()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return run_command(args)
        if args.command == "report":
            return report_command(args)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    parser.error(f"unknown command: {args.command}")
    return 2

