from __future__ import annotations

from eso_object_types.taxonomy import (
    classify_catalog_object,
)


def row(**values):
    defaults = {
        "catalog": "simbad",
        "primary_type_code": None,
        "type_label": None,
        "type_description": None,
        "type_path": None,
        "type_is_candidate": None,
        "spectral_type": None,
        "morphological_type": None,
    }
    defaults.update(values)
    return defaults


def test_stellar_spectral_type_adds_ob_subcategory() -> None:
    assertion = classify_catalog_object(
        row(
            primary_type_code="*",
            type_label="Star",
            type_description="Star",
            type_path="*",
            spectral_type="B0 V",
        )
    )
    assert assertion.broad_category == "Star"
    assert assertion.subcategory == "OB star"
    assert assertion.detail == "B0 V"


def test_specific_galaxy_type_and_morphology() -> None:
    simbad = classify_catalog_object(
        row(
            primary_type_code="Sy1",
            type_label="Seyfert1",
            type_description="Seyfert 1 Galaxy",
            type_path="G > AGN > SyG > Sy1",
            morphological_type="SBb",
        )
    )
    assert simbad.broad_category == "Galaxy"
    assert simbad.subcategory == "Seyfert 1 Galaxy"
    assert simbad.detail == "SBb"


def test_supernova_candidate_and_main_class_subcategory() -> None:
    candidate = classify_catalog_object(
        row(
            primary_type_code="SN?",
            type_label="Supernova_Candidate",
            type_description="SuperNova Candidate",
            type_path="* > SN*",
            type_is_candidate=1,
        )
    )
    confirmed = classify_catalog_object(
        row(
            primary_type_code="SN*",
            type_label="Supernova",
            type_description="SuperNova",
            type_path="* > SN*",
            type_is_candidate=0,
        )
    )
    assert candidate.broad_category == "Supernova"
    assert candidate.subcategory == "Candidate supernova"
    assert confirmed.subcategory == "Supernova"


def test_generic_types_use_the_main_class_without_extra_specificity() -> None:
    for code, category in [("*", "Star"), ("G", "Galaxy"), ("SN*", "Supernova")]:
        assertion = classify_catalog_object(
            row(primary_type_code=code, type_label=category, type_description=category)
        )
        assert assertion.subcategory == category
        assert assertion.specificity == 1

    unknown = classify_catalog_object(row(primary_type_code="?"))
    assert unknown.broad_category == "Unknown"
    assert unknown.subcategory is None


def test_explicit_codes_cover_the_remaining_broad_categories() -> None:
    expected = {
        "ev": "Other transient",
        "HII": "Nebula or ISM",
        "Cl*": "Star cluster or association",
        "GGroup": "Galaxy group or cluster",
        "Psr": "Compact object",
        "Pl": "Solar-system object",
        "unmapped-code": "Other",
        "?": "Unknown",
    }
    assert {
        code: classify_catalog_object(
            row(primary_type_code=code, type_label=code)
        ).broad_category
        for code in expected
    } == expected
