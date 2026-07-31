from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import astropy.units as u
from astropy.coordinates import SkyCoord

from .models import BestObject, BestObjectMember
from .taxonomy import (
    TAXONOMY_VERSION,
    classify_catalog_object,
    combine_classifications,
)

if TYPE_CHECKING:
    from .database import Database

RANKING_VERSION = "v1"

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
class _CandidateGroup:
    members: list[_Candidate] = field(default_factory=list)
    name_level: int = 0
    match_method: str = "position"
    target_variant: str | None = None
    normalized_separation: float = math.inf
    classification_specificity: int = 0

    @property
    def separation_arcsec(self) -> float:
        return min(member.separation_arcsec for member in self.members)

    @property
    def catalogs(self) -> set[str]:
        return {member.catalog for member in self.members}

    @property
    def object_key(self) -> str:
        identities = sorted(
            f"{member.catalog}:{member.object_id}" for member in self.members
        )
        digest = hashlib.sha256("|".join(identities).encode("utf-8")).hexdigest()
        return f"resolved-{digest[:16]}"


def _catalog_separation(first: _Candidate, second: _Candidate) -> float:
    first_position = SkyCoord(
        float(first.row["ra_deg"]) * u.deg,
        float(first.row["dec_deg"]) * u.deg,
    )
    second_position = SkyCoord(
        float(second.row["ra_deg"]) * u.deg,
        float(second.row["dec_deg"]) * u.deg,
    )
    return float(first_position.separation(second_position).arcsec)


def _group_candidates(candidates: list[_Candidate]) -> list[_CandidateGroup]:
    simbad = [item for item in candidates if item.catalog == "simbad"]
    ned = [item for item in candidates if item.catalog == "ned"]
    groups = {
        item.object_id: _CandidateGroup(members=[item]) for item in simbad
    }
    simbad_by_name: dict[str, list[_Candidate]] = defaultdict(list)
    for simbad_item in simbad:
        for name in simbad_item.names:
            simbad_by_name[name].append(simbad_item)

    possible_pairs: list[tuple[float, str, str]] = []
    ambiguous_ned: set[str] = set()
    for ned_item in ned:
        preferred_name = normalize_name(ned_item.preferred_name)
        matches = simbad_by_name.get(preferred_name, []) if preferred_name else []
        if not matches:
            continue
        distances = sorted(
            (_catalog_separation(ned_item, item), item.object_id)
            for item in matches
        )
        if len(distances) > 1 and math.isclose(
            distances[0][0], distances[1][0], abs_tol=1e-6
        ):
            ambiguous_ned.add(ned_item.object_id)
            continue
        possible_pairs.append(
            (distances[0][0], ned_item.object_id, distances[0][1])
        )

    ned_by_id = {item.object_id: item for item in ned}
    used_ned: set[str] = set()
    used_simbad: set[str] = set()
    for _, ned_id, simbad_id in sorted(possible_pairs):
        if (
            ned_id in used_ned
            or simbad_id in used_simbad
            or ned_id in ambiguous_ned
        ):
            continue
        groups[simbad_id].members.append(ned_by_id[ned_id])
        used_ned.add(ned_id)
        used_simbad.add(simbad_id)

    result = list(groups.values())
    result.extend(
        _CandidateGroup(members=[item])
        for item in ned
        if item.object_id not in used_ned
    )
    return result


def _representative(group: _CandidateGroup) -> _Candidate:
    return min(
        group.members,
        key=lambda member: (
            member.catalog != "simbad",
            member.separation_arcsec,
            member.preferred_name or "",
            member.object_id,
        ),
    )


def _raw_types(group: _CandidateGroup) -> str:
    return ";".join(
        f"{member.catalog}:{member.row['primary_type_code'] or 'UNKNOWN'}"
        for member in sorted(
            group.members, key=lambda item: (item.catalog, item.object_id)
        )
    )


def _resolve_observation(
    *,
    run_id: str,
    observation: Any,
    candidates: list[_Candidate],
) -> tuple[BestObject, list[BestObjectMember]]:
    observation_id = str(observation["eso_dp_id"])
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
                classification_conflict=False,
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
    groups = _group_candidates(candidates)

    classifications: dict[str, Any] = {}
    for group in groups:
        all_names = set().union(*(member.names for member in group.members))
        if original_name and original_name in all_names:
            group.name_level = 2
            group.match_method = "target_name"
            group.target_variant = str(target_name)
        elif base_name and base_name in all_names:
            group.name_level = 1
            group.match_method = "target_name_base"
            group.target_variant = base_text
        group.normalized_separation = (
            group.separation_arcsec / radius_arcsec
            if radius_arcsec > 0
            else math.inf
        )
        classification = combine_classifications(
            [classify_catalog_object(member.row) for member in group.members]
        )
        classifications[group.object_key] = classification
        group.classification_specificity = classification.specificity

    groups.sort(
        key=lambda group: (
            -group.name_level,
            group.normalized_separation,
            -len(group.catalogs),
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

    alias_complete = all(
        member.catalog != "simbad" or bool(member.row["aliases_complete"])
        for member in candidates
    )
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
    if not alias_complete or classification.conflict:
        confidence = "low"

    representative = _representative(best)
    members = [
        BestObjectMember(
            run_id=run_id,
            eso_dp_id=observation_id,
            catalog=member.catalog,
            catalog_object_id=member.object_id,
            member_role=(
                "primary"
                if member.catalog == representative.catalog
                and member.object_id == representative.object_id
                else "supporting"
            ),
        )
        for member in best.members
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
            supporting_catalogs=",".join(sorted(best.catalogs)),
            raw_catalog_types=_raw_types(best),
            classification_conflict=classification.conflict,
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
        SELECT oo.eso_dp_id, oo.separation_arcsec,
               co.catalog, co.catalog_object_id, co.preferred_name,
               co.ra_deg, co.dec_deg, co.primary_type_code,
               co.catalog_type_key, co.spectral_type, co.morphological_type,
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
