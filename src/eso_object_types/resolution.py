from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .models import BestObject, BestObjectMember
from .taxonomy import (
    TAXONOMY_VERSION,
    classify_catalog_object,
)

if TYPE_CHECKING:
    from .database import Database

RANKING_VERSION = "v2"

_GENERIC_NAMES = {
    "",
    "blank",
    "field",
    "none",
    "object",
    "off",
    "offset",
    "sky",
    "target",
    "test",
    "unknown",
}
_TRAILING_ANNOTATION = re.compile(
    r"""(?ix)
    (?:[_\-\s]+)
    (?:
        offset(?:[_\-\s]*(?:north|south|east|west|n|s|e|w|\d+))?
      | off(?:[_\-\s]*(?:north|south|east|west|n|s|e|w|\d+))?
      | sky
      | blank
      | pointing\d*
      | pos\d+
      | mosaic\d*
    )$
    """
)


def normalize_name(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def target_name_variants(value: str | None) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    original_text = value.strip()
    original = normalize_name(original_text)
    if original in _GENERIC_NAMES:
        original = ""

    base_text = _TRAILING_ANNOTATION.sub("", original_text).strip("_- ")
    base = normalize_name(base_text)
    if base in _GENERIC_NAMES or base == original:
        base = ""
    return original or None, base or None


def _base_target_text(value: str | None) -> str | None:
    if not value:
        return None
    base = _TRAILING_ANNOTATION.sub("", value.strip()).strip("_- ")
    return base or None


@dataclass
class _Candidate:
    row: Any
    aliases: tuple[str, ...]

    @property
    def catalog(self) -> str:
        return str(self.row["catalog"])

    @property
    def object_id(self) -> str:
        return str(self.row["catalog_object_id"])

    @property
    def preferred_name(self) -> str | None:
        value = self.row["preferred_name"]
        return None if value is None else str(value)

    @property
    def separation_arcsec(self) -> float:
        return float(self.row["separation_arcsec"])

    @property
    def names(self) -> set[str]:
        values = {normalize_name(self.preferred_name)}
        values.update(normalize_name(alias) for alias in self.aliases)
        return {value for value in values if value}


@dataclass
class _RankedCandidate:
    candidate: _Candidate
    name_level: int = 0
    match_method: str = "position"
    target_variant: str | None = None
    normalized_separation: float = math.inf
    classification_specificity: int = 0

    @property
    def separation_arcsec(self) -> float:
        return self.candidate.separation_arcsec

    @property
    def object_key(self) -> str:
        identity = f"{self.candidate.catalog}:{self.candidate.object_id}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return f"resolved-{digest[:16]}"


def _resolve_observation(
    *,
    run_id: str,
    observation: Any,
    candidates: list[_Candidate],
) -> tuple[BestObject, list[BestObjectMember]]:
    observation_id = str(observation["eso_dp_id"])
    positional = [item for item in candidates if item.row["match_method"] == "position"]
    if positional:
        candidates = positional
    if not candidates:
        return (
            BestObject(
                run_id=run_id,
                eso_dp_id=observation_id,
                best_object_key=None,
                best_object_name=None,
                broad_category="Unknown",
                subcategory=None,
                classification_detail=None,
                confidence="none",
                match_method="no_catalog_match",
                target_name_variant=None,
                separation_arcsec=None,
                normalized_separation=None,
                candidate_group_count=0,
                runner_up_margin=None,
                supporting_catalogs=None,
                raw_catalog_types=None,
                alias_complete=True,
                ranking_version=RANKING_VERSION,
                taxonomy_version=TAXONOMY_VERSION,
            ),
            [],
        )

    target_name = observation["target_name"]
    original_name, base_name = target_name_variants(target_name)
    base_text = _base_target_text(target_name)
    radius_arcsec = float(observation["search_radius_deg"]) * 3600.0
    groups = [_RankedCandidate(candidate=item) for item in candidates]

    classifications: dict[str, Any] = {}
    for group in groups:
        all_names = group.candidate.names
        if original_name and original_name in all_names:
            group.name_level = 2
            group.match_method = "target_name"
            group.target_variant = str(target_name)
        elif base_name and base_name in all_names:
            group.name_level = 1
            group.match_method = "target_name_base"
            group.target_variant = base_text
        if group.candidate.row["match_method"] == "target_name_fallback":
            group.match_method = "target_name_fallback"
            group.target_variant = str(target_name)
        group.normalized_separation = (
            group.separation_arcsec / radius_arcsec
            if radius_arcsec > 0
            else math.inf
        )
        classification = classify_catalog_object(group.candidate.row)
        classifications[group.object_key] = classification
        group.classification_specificity = classification.specificity

    groups.sort(
        key=lambda group: (
            -group.name_level,
            group.normalized_separation,
            -group.classification_specificity,
            group.object_key,
        )
    )
    best = groups[0]
    second = groups[1] if len(groups) > 1 else None
    runner_up_margin = (
        None
        if second is None
        else second.normalized_separation - best.normalized_separation
    )

    alias_complete = all(bool(item.row["aliases_complete"]) for item in candidates)
    same_name_level = sum(
        group.name_level == best.name_level and group.name_level > 0
        for group in groups
    )
    if best.name_level > 0 and same_name_level == 1:
        confidence = "high"
    elif len(groups) == 1:
        confidence = "medium"
    elif (
        best.normalized_separation <= 0.5
        and runner_up_margin is not None
        and runner_up_margin >= 0.25
    ):
        confidence = "medium"
    else:
        confidence = "low"

    classification = classifications[best.object_key]
    if not alias_complete or best.match_method == "target_name_fallback":
        confidence = "low"

    representative = best.candidate
    members = [
        BestObjectMember(
            run_id=run_id,
            eso_dp_id=observation_id,
            catalog=representative.catalog,
            catalog_object_id=representative.object_id,
            member_role="primary",
        )
    ]
    return (
        BestObject(
            run_id=run_id,
            eso_dp_id=observation_id,
            best_object_key=best.object_key,
            best_object_name=representative.preferred_name,
            broad_category=classification.broad_category,
            subcategory=classification.subcategory,
            classification_detail=classification.detail,
            confidence=confidence,
            match_method=best.match_method,
            target_name_variant=best.target_variant,
            separation_arcsec=best.separation_arcsec,
            normalized_separation=best.normalized_separation,
            candidate_group_count=len(groups),
            runner_up_margin=runner_up_margin,
            supporting_catalogs=representative.catalog,
            raw_catalog_types=(
                f"{representative.catalog}:"
                f"{representative.row['primary_type_code'] or 'UNKNOWN'}"
            ),
            alias_complete=alias_complete,
            ranking_version=RANKING_VERSION,
            taxonomy_version=TAXONOMY_VERSION,
        ),
        members,
    )


def rebuild_best_objects(database: Database, run_id: str) -> int:
    connection = database.connection
    observations = connection.execute(
        """
        SELECT o.*
        FROM observations AS o
        JOIN run_observations AS ro USING (eso_dp_id)
        WHERE ro.run_id = ?
        ORDER BY o.eso_dp_id
        """,
        (run_id,),
    ).fetchall()
    candidate_rows = connection.execute(
        """
        SELECT oo.eso_dp_id, oo.separation_arcsec, oo.match_method,
               co.catalog, co.catalog_object_id, co.preferred_name,
               co.ra_deg, co.dec_deg, co.primary_type_code,
               co.spectral_type, co.morphological_type,
               CASE
                   WHEN co.aliases_retrieved_at IS NOT NULL
                     OR EXISTS (
                         SELECT 1
                         FROM catalog_object_aliases AS cached_alias
                         WHERE cached_alias.catalog = co.catalog
                           AND cached_alias.catalog_object_id =
                               co.catalog_object_id
                     )
                   THEN 1
                   ELSE 0
               END AS aliases_complete,
               ot.type_label, ot.type_description, ot.type_path,
               ot.type_is_candidate
        FROM observation_objects AS oo
        JOIN run_observations AS ro USING (eso_dp_id)
        JOIN catalog_objects AS co
          ON co.catalog = oo.catalog
         AND co.catalog_object_id = oo.catalog_object_id
        LEFT JOIN object_types AS ot
          ON ot.catalog = co.catalog
         AND ot.type_code = co.primary_type_code
        WHERE ro.run_id = ?
        ORDER BY oo.eso_dp_id, co.catalog, co.catalog_object_id
        """,
        (run_id,),
    ).fetchall()
    aliases = defaultdict(list)
    for row in connection.execute(
        """
        SELECT ca.catalog, ca.catalog_object_id, ca.alias
        FROM catalog_object_aliases AS ca
        JOIN (
            SELECT DISTINCT oo.catalog, oo.catalog_object_id
            FROM observation_objects AS oo
            JOIN run_observations AS ro USING (eso_dp_id)
            WHERE ro.run_id = ?
        ) AS run_objects
          ON run_objects.catalog = ca.catalog
         AND run_objects.catalog_object_id = ca.catalog_object_id
        """,
        (run_id,),
    ):
        aliases[(row["catalog"], row["catalog_object_id"])].append(row["alias"])

    by_observation: dict[str, list[_Candidate]] = defaultdict(list)
    for row in candidate_rows:
        key = (row["catalog"], row["catalog_object_id"])
        by_observation[str(row["eso_dp_id"])].append(
            _Candidate(row=row, aliases=tuple(aliases[key]))
        )

    best_objects: list[BestObject] = []
    members: list[BestObjectMember] = []
    for observation in observations:
        best, best_members = _resolve_observation(
            run_id=run_id,
            observation=observation,
            candidates=by_observation[str(observation["eso_dp_id"])],
        )
        best_objects.append(best)
        members.extend(best_members)
    database.replace_best_objects(run_id, best_objects, members)
    return len(best_objects)
