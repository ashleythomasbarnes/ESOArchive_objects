from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TAXONOMY_VERSION = "v2"

BROAD_CATEGORIES = (
    "Star",
    "Galaxy",
    "Supernova",
    "Other transient",
    "Nebula or ISM",
    "Star cluster or association",
    "Galaxy group or cluster",
    "Compact object",
    "Solar-system object",
    "Other",
    "Unknown",
)

_SUPERNOVA_CODES = {"SN", "SN*", "SN?"}
_TRANSIENT_CODES = {"EV", "GRB", "NOVA", "NOV", "NO*", "TRANSIENT"}
_GALAXY_GROUP_CODES = {"GGROUP", "GRG", "CLG", "CGG", "PAG"}
_NEBULA_CODES = {"HII", "HIIREGION", "PN", "SNR", "ISM"}
_STAR_CLUSTER_CODES = {"CL*", "OPC", "GLC", "GCL", "AS*", "ASSOC"}
_COMPACT_CODES = {"BH", "BH?", "NS", "PSR", "PULSAR"}
_SOLAR_CODES = {
    "PL",
    "PLANET",
    "DPL",
    "COM",
    "COMET",
    "AST",
    "ASTEROID",
    "MOO",
    "SSO",
}
_UNKNOWN_CODES = {"", "?", "UNKNOWN"}


@dataclass(frozen=True)
class TypeAssertion:
    catalog: str
    raw_code: str | None
    broad_category: str
    subcategory: str | None
    detail: str | None
    specificity: int
    is_candidate: bool


def _value(row: Any, name: str) -> Any:
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _specific_description(
    description: str | None, generic_values: set[str]
) -> str | None:
    if description is None or description.casefold() in generic_values:
        return None
    return description


def classify_catalog_object(row: Any) -> TypeAssertion:
    catalog = str(_value(row, "catalog"))
    code = _text(_value(row, "primary_type_code"))
    label = _text(_value(row, "type_label"))
    description = _text(_value(row, "type_description"))
    path = _text(_value(row, "type_path"))
    spectral_type = _text(_value(row, "spectral_type"))
    morphology = _text(_value(row, "morphological_type"))
    is_candidate = bool(_value(row, "type_is_candidate") or False)

    code_upper = (code or "").upper()
    description_lower = (description or "").casefold()
    path_root = (path or "").split(">", maxsplit=1)[0].strip()

    if code_upper in _UNKNOWN_CODES or "unknown nature" in description_lower:
        category = "Unknown"
    elif code_upper in _SUPERNOVA_CODES or "supernova" in description_lower:
        category = "Supernova"
    elif code_upper in _TRANSIENT_CODES or description_lower == "transient event":
        category = "Other transient"
    elif code_upper in _GALAXY_GROUP_CODES:
        category = "Galaxy group or cluster"
    elif code_upper in _NEBULA_CODES or path_root.upper() == "ISM":
        category = "Nebula or ISM"
    elif code_upper in _STAR_CLUSTER_CODES:
        category = "Star cluster or association"
    elif code_upper in _COMPACT_CODES:
        category = "Compact object"
    elif code_upper in _SOLAR_CODES:
        category = "Solar-system object"
    elif path_root == "G" or code_upper in {"G", "AGN", "QSO"}:
        category = "Galaxy"
    elif path_root == "*" or code_upper in {"*", "V*"}:
        category = "Star"
    elif code is None:
        category = "Unknown"
    else:
        category = "Other"

    subtype: str | None = None
    detail: str | None = None
    if category == "Supernova":
        subtype = "Candidate supernova" if is_candidate else category
    elif category == "Star":
        subtype = _specific_description(
            description, {"a star", "star", "stellar object"}
        )
        if subtype is None and spectral_type:
            first = spectral_type.lstrip()[:1].upper()
            subtype = "OB star" if first in {"O", "B"} else None
        if subtype is None:
            subtype = _specific_description(
                label, {"a star", "star", "stellar object"}
            )
        detail = spectral_type
    elif category == "Galaxy":
        subtype = _specific_description(
            description, {"galaxy", "extragalactic object"}
        )
        if subtype is None:
            subtype = _specific_description(
                label, {"galaxy", "extragalactic object", "g"}
            )
        detail = morphology
    elif category not in {"Unknown", "Other"}:
        subtype = description or label or code
    elif category == "Other":
        subtype = description or label or code

    if subtype is None and category not in {"Unknown", "Other"}:
        subtype = category

    specificity = 0
    if category not in {"Unknown", "Other"}:
        specificity += 1
    if subtype and subtype.casefold() != category.casefold():
        specificity += 1
    if detail:
        specificity += 1
    if path:
        specificity += path.count(">")

    return TypeAssertion(
        catalog=catalog,
        raw_code=code,
        broad_category=category,
        subcategory=subtype,
        detail=detail,
        specificity=specificity,
        is_candidate=is_candidate,
    )
