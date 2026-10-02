from __future__ import annotations

import hashlib
import json
import random
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .database import Database
from .geometry import batch_hash, chunked, consolidate_search_targets
from .logging_utils import EventLogger
from .models import BatchResult, Observation, RunConfig, SearchTarget
from .reports import build_summary, export_run
from .resolution import rebuild_best_objects, target_name_variants
from .services import EsoClient, SimbadClient


def new_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def observation_from_row(row: Any) -> Observation:
    return Observation(
        eso_dp_id=str(row["eso_dp_id"]),
        obs_publisher_did=row["obs_publisher_did"],
        target_name=row["target_name"],
        ra_deg=float(row["ra_deg"]),
        dec_deg=float(row["dec_deg"]),
        s_fov_deg=row["s_fov_deg"],
        s_region=row["s_region"],
        search_radius_deg=float(row["search_radius_deg"]),
        instrument_name=row["instrument_name"],
        access_url=row["access_url"],
        healpix_order10=int(row["healpix_order10"]),
    )


def retry_after_seconds(error: Exception) -> float | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        value = headers.get("Retry-After")
        if value:
            try:
                return max(0.0, float(value))
            except ValueError:
                return None
    return None


class Pipeline:
    def __init__(
        self,
        database: Database,
        config: RunConfig,
        eso_factory: Callable[[str], Any] = EsoClient,
        simbad_factory: Callable[[str], Any] = SimbadClient,
        sleep: Callable[[float], None] = time.sleep,
        random_source: Callable[[], float] = random.random,
    ):
        self.database = database
        self.config = config
        self.eso_factory = eso_factory
        self.simbad_factory = simbad_factory
        self.sleep = sleep
        self.random_source = random_source

    def _call_with_retries(
        self,
        *,
        run_id: str,
        logger: EventLogger,
        service: str,
        call_hash: str,
        operation: Callable[[], Any],
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, self.config.retries + 1):
            self.database.increment_service_attempt(run_id, service, call_hash)
            try:
                return operation()
            except Exception as error:
                last_error = error
                logger.warning(
                    "service_retry",
                    (
                        f"{service} attempt {attempt}/{self.config.retries} "
                        f"failed: {type(error).__name__}: {str(error)[:300]}"
                    ),
                    service=service,
                    batch_hash=call_hash,
                    attempt=attempt,
                    error_type=type(error).__name__,
                )
                if attempt == self.config.retries:
                    break
                delay = retry_after_seconds(error)
                if delay is None:
                    delay = min(60.0, 2.0 ** (attempt - 1))
                    delay += self.random_source()
                self.sleep(delay)
        assert last_error is not None
        raise last_error

    def _fetch_eso(
        self, run_id: str, logger: EventLogger
    ) -> tuple[list[Observation], bool]:
        call_hash = f"sample-{self.config.limit}-radius-{self.config.min_radius_arcsec}"
        status = self.database.service_call_status(run_id, "eso", call_hash)
        existing = self.database.observations_for_run(run_id)
        if status == "completed" and existing:
            logger.info(
                "batch_skip",
                f"ESO sample already completed, reusing {len(existing)} rows",
                service="eso",
                batch_hash=call_hash,
            )
            return [observation_from_row(row) for row in existing], True

        self.database.start_service_call(
            run_id, "eso", call_hash, batch_number=1, input_count=self.config.limit
        )
        started = time.monotonic()
        try:
            client = self.eso_factory(self.config.eso_endpoint)
            observations = self._call_with_retries(
                run_id=run_id,
                logger=logger,
                service="eso",
                call_hash=call_hash,
                operation=lambda: client.fetch_observations(
                    self.config.limit, self.config.min_radius_arcsec
                ),
            )
            if len(observations) != self.config.limit:
                raise RuntimeError(
                    f"ESO returned {len(observations)} rows, expected "
                    f"{self.config.limit}"
                )
            elapsed = time.monotonic() - started
            self.database.complete_eso_call(
                run_id, call_hash, elapsed, observations
            )
            logger.info(
                "eso_complete",
                f"Retrieved {len(observations)} reduced-spectrum metadata rows",
                service="eso",
                batch_hash=call_hash,
                result_count=len(observations),
                elapsed_seconds=round(elapsed, 3),
            )
            return list(observations), True
        except Exception as error:
            elapsed = time.monotonic() - started
            self.database.fail_service_call(
                run_id, "eso", call_hash, elapsed, error
            )
            logger.error(
                "eso_failed",
                f"ESO sample failed: {type(error).__name__}: {str(error)[:300]}",
                service="eso",
                batch_hash=call_hash,
            )
            return [], False

    def _process_catalog(
        self,
        *,
        run_id: str,
        logger: EventLogger,
        service: str,
        targets: Sequence[SearchTarget],
        batch_size: int,
        client_factory: Callable[[str], Any],
        endpoint: str,
        query_method: str = "query_batch",
    ) -> bool:
        successful = True
        client: Any | None = None
        batches = list(chunked(targets, batch_size))
        for batch_number, batch in enumerate(batches, start=1):
            call_hash = batch_hash(service, batch)
            if (
                self.database.service_call_status(run_id, service, call_hash)
                == "completed"
            ):
                logger.info(
                    "batch_skip",
                    f"{service} batch {batch_number}/{len(batches)} already completed",
                    service=service,
                    batch_hash=call_hash,
                    batch_number=batch_number,
                )
                continue

            self.database.start_service_call(
                run_id,
                service,
                call_hash,
                batch_number=batch_number,
                input_count=len(batch),
            )
            started = time.monotonic()
            try:
                if client is None:
                    client = client_factory(endpoint)
                result: BatchResult = self._call_with_retries(
                    run_id=run_id,
                    logger=logger,
                    service=service,
                    call_hash=call_hash,
                    operation=lambda current=batch: getattr(client, query_method)(current),
                )
                elapsed = time.monotonic() - started
                observation_ids = sorted(
                    {
                        observation_id
                        for target in batch
                        for observation_id in target.observation_ids
                    }
                )
                self.database.complete_catalog_call(
                    run_id,
                    service,
                    call_hash,
                    elapsed,
                    result,
                    observation_ids,
                )
                logger.info(
                    "batch_complete",
                    (
                        f"{service} batch {batch_number}/{len(batches)}: "
                        f"{len(result.objects)} objects, {len(result.matches)} links"
                    ),
                    service=service,
                    batch_hash=call_hash,
                    batch_number=batch_number,
                    input_count=len(batch),
                    object_count=len(result.objects),
                    result_count=len(result.matches),
                    elapsed_seconds=round(elapsed, 3),
                )
            except Exception as error:
                successful = False
                elapsed = time.monotonic() - started
                self.database.fail_service_call(
                    run_id, service, call_hash, elapsed, error
                )
                logger.error(
                    "batch_failed",
                    (
                        f"{service} batch {batch_number}/{len(batches)} failed: "
                        f"{type(error).__name__}: {str(error)[:300]}"
                    ),
                    service=service,
                    batch_hash=call_hash,
                    batch_number=batch_number,
                    error_type=type(error).__name__,
                )
        return successful

    def _process_simbad_names(
        self, run_id: str, logger: EventLogger, observations: Sequence[Observation]
    ) -> bool:
        positional_ids = {
            str(row[0])
            for row in self.database.connection.execute(
                """
                SELECT DISTINCT oo.eso_dp_id FROM observation_objects AS oo
                JOIN run_observations AS ro USING (eso_dp_id)
                WHERE ro.run_id = ? AND oo.catalog = 'simbad'
                  AND oo.match_method = 'position'
                """,
                (run_id,),
            )
        }
        grouped: dict[tuple[str, float, float, float], list[str]] = {}
        for observation in observations:
            if observation.eso_dp_id in positional_ids:
                continue
            original, _ = target_name_variants(observation.target_name)
            if not original:
                continue
            key = (
                observation.target_name.strip(), observation.ra_deg,
                observation.dec_deg, observation.search_radius_deg,
            )
            grouped.setdefault(key, []).append(observation.eso_dp_id)
        targets = [
            SearchTarget(
                search_key=hashlib.sha256(json.dumps(key).encode()).hexdigest()[:32],
                target_name=key[0], ra_deg=key[1], dec_deg=key[2], radius_deg=key[3],
                observation_ids=tuple(sorted(grouped[key])),
            )
            for key in sorted(grouped)
        ]
        logger.info(
            "name_fallback_start",
            f"Checking {len(targets)} unmatched ESO target names in SIMBAD",
            service="simbad_name", input_count=len(targets),
        )
        return self._process_catalog(
            run_id=run_id, logger=logger, service="simbad_name", targets=targets,
            batch_size=self.config.simbad_batch_size,
            client_factory=self.simbad_factory, endpoint=self.config.simbad_endpoint,
            query_method="query_name_batch",
        )

    def _process_simbad_aliases(
        self, run_id: str, logger: EventLogger
    ) -> bool:
        object_ids = self.database.uncached_simbad_object_ids_for_run(run_id)
        if not object_ids:
            logger.info(
                "aliases_complete",
                "No uncached SIMBAD object aliases are required",
                service="simbad_alias",
                result_count=0,
            )
            return True

        batch_size = self.config.simbad_alias_batch_size
        batches = [
            object_ids[start : start + batch_size]
            for start in range(0, len(object_ids), batch_size)
        ]
        client: Any | None = None
        successful = True
        for batch_number, batch in enumerate(batches, start=1):
            encoded = "|".join(batch).encode("utf-8")
            call_hash = hashlib.sha256(
                b"simbad_alias|" + encoded
            ).hexdigest()
            if (
                self.database.service_call_status(
                    run_id, "simbad_alias", call_hash
                )
                == "completed"
            ):
                logger.info(
                    "batch_skip",
                    (
                        f"SIMBAD alias batch {batch_number}/{len(batches)} "
                        "already completed"
                    ),
                    service="simbad_alias",
                    batch_hash=call_hash,
                    batch_number=batch_number,
                )
                continue

            self.database.start_service_call(
                run_id,
                "simbad_alias",
                call_hash,
                batch_number=batch_number,
                input_count=len(batch),
            )
            started = time.monotonic()
            try:
                if client is None:
                    client = self.simbad_factory(self.config.simbad_endpoint)
                aliases = self._call_with_retries(
                    run_id=run_id,
                    logger=logger,
                    service="simbad_alias",
                    call_hash=call_hash,
                    operation=lambda current=batch: client.query_aliases(current),
                )
                elapsed = time.monotonic() - started
                self.database.complete_alias_call(
                    run_id, call_hash, elapsed, batch, aliases
                )
                logger.info(
                    "batch_complete",
                    (
                        f"SIMBAD alias batch {batch_number}/{len(batches)}: "
                        f"{len(aliases)} aliases"
                    ),
                    service="simbad_alias",
                    batch_hash=call_hash,
                    batch_number=batch_number,
                    input_count=len(batch),
                    result_count=len(aliases),
                    elapsed_seconds=round(elapsed, 3),
                )
            except Exception as error:
                successful = False
                elapsed = time.monotonic() - started
                self.database.fail_service_call(
                    run_id, "simbad_alias", call_hash, elapsed, error
                )
                logger.error(
                    "batch_failed",
                    (
                        f"SIMBAD alias batch {batch_number}/{len(batches)} "
                        f"failed: {type(error).__name__}: {str(error)[:300]}"
                    ),
                    service="simbad_alias",
                    batch_hash=call_hash,
                    batch_number=batch_number,
                    error_type=type(error).__name__,
                )
        return successful

    def run(self, resume_run: str | None = None) -> tuple[str, int, dict[str, Any]]:
        self.config.validate()
        if resume_run:
            run_id = resume_run
            row = self.database.get_run(run_id)
            if row is None:
                raise ValueError(f"run {run_id!r} does not exist")
            stored_config = RunConfig.from_dict(json.loads(row["config_json"]))
            self.config = stored_config
            self.config.validate()
        else:
            run_id = new_run_id()
            self.database.create_run(run_id, self.config)

        logger = EventLogger(run_id, self.config.output_dir)
        try:
            logger.info(
                "run_start",
                f"Starting ESO object-type run {run_id}",
                config=self.config.to_dict(),
                resumed=bool(resume_run),
            )
            observations, eso_success = self._fetch_eso(run_id, logger)
            catalogs_success = False
            aliases_success = False
            if eso_success:
                targets = consolidate_search_targets(observations)
                logger.info(
                    "search_consolidated",
                    (
                        f"Consolidated {len(observations)} observations into "
                        f"{len(targets)} unique search positions"
                    ),
                    observation_count=len(observations),
                    unique_search_count=len(targets),
                )
                simbad_success = self._process_catalog(
                    run_id=run_id,
                    logger=logger,
                    service="simbad",
                    targets=targets,
                    batch_size=self.config.simbad_batch_size,
                    client_factory=self.simbad_factory,
                    endpoint=self.config.simbad_endpoint,
                )
                catalogs_success = simbad_success
                if simbad_success:
                    catalogs_success = self._process_simbad_names(
                        run_id, logger, observations
                    )
                aliases_success = self._process_simbad_aliases(run_id, logger)
                resolved_count = rebuild_best_objects(self.database, run_id)
                logger.info(
                    "best_objects_complete",
                    f"Resolved one best-object row for {resolved_count} observations",
                    result_count=resolved_count,
                )

            status = (
                "completed"
                if eso_success and catalogs_success and aliases_success
                else "partial"
            )
            summary = build_summary(
                self.database.connection,
                run_id,
                self.config.simbad_batch_size,
            )
            self.database.finish_run(run_id, status, summary)
            export_path, summary = export_run(
                self.database.connection,
                run_id,
                self.config.output_dir,
                self.config.simbad_batch_size,
            )
            logger.info(
                "run_finish",
                (
                    f"Run {run_id} finished with status {status}; "
                    f"exports: {export_path}"
                ),
                status=status,
                summary=summary,
                export_path=str(export_path),
            )
            return run_id, 0 if status == "completed" else 2, summary
        finally:
            logger.close()
