from __future__ import annotations

from astropy.table import Table

from eso_object_types.models import SearchTarget
from eso_object_types.services import (
    build_eso_query,
    build_ned_query,
    build_simbad_alias_query,
    build_simbad_query,
    map_ned_results,
    map_simbad_results,
)


def targets() -> list[SearchTarget]:
    return [
        SearchTarget(
            search_key="q00000001",
            ra_deg=10.0,
            dec_deg=-20.0,
            radius_deg=2.0 / 3600.0,
            observation_ids=("ESO-A", "ESO-B"),
        ),
        SearchTarget(
            search_key="q00000002",
            ra_deg=11.0,
            dec_deg=-20.0,
            radius_deg=1.0 / 3600.0,
            observation_ids=("ESO-C",),
        ),
    ]


def test_queries_are_batched_and_constrained() -> None:
    eso = build_eso_query(50)
    assert "TOP 50" in eso
    assert "dataproduct_type = 'spectrum'" in eso
    assert "ORDER BY dp_id" in eso

    simbad = build_simbad_query()
    assert "TAP_UPLOAD.targets" in simbad
    assert "u.radius_deg" in simbad
    assert "otypedef" in simbad
    assert "b.sp_type" in simbad
    assert "b.morph_type" in simbad
    assert "d.path" in simbad
    assert "d.is_candidate" in simbad

    aliases = build_simbad_alias_query()
    assert "TAP_UPLOAD.objects" in aliases
    assert "ident" in aliases

    ned = build_ned_query(targets())
    assert ned.count("CONTAINS(") == 2
    assert "\n OR " in ned
    assert "NEDTAP.objdir" in ned


def test_simbad_mapping_expands_consolidated_observations() -> None:
    table = Table(
        rows=[
            (
                "q00000001",
                100,
                "Target",
                10.0,
                -20.0,
                "Star",
                "Star",
                "A star",
                "* > Star",
                0,
                "B0 V",
                None,
                0.25,
            )
        ],
        names=(
            "search_key",
            "oid",
            "main_id",
            "ra",
            "dec",
            "otype",
            "label",
            "description",
            "path",
            "is_candidate",
            "sp_type",
            "morph_type",
            "separation_arcsec",
        ),
    )
    result = map_simbad_results(targets(), table)
    assert len(result.objects) == 1
    assert {match.eso_dp_id for match in result.matches} == {"ESO-A", "ESO-B"}
    assert result.objects[0].catalog_object_id == "100"
    assert result.objects[0].primary_type_path == "* > Star"
    assert result.objects[0].spectral_type == "B0 V"


def test_ned_mapping_filters_union_to_each_cone() -> None:
    table = Table(
        rows=[
            (200, "Near", 10.0001, -20.0, "G", 3),
            (201, "Only second", 11.0001, -20.0, "QSO", 4),
            (202, "Outside", 10.01, -20.0, "G", 3),
        ],
        names=("objid", "prefname", "ra", "dec", "prefphytype", "type_key"),
    )
    result = map_ned_results(targets(), table)
    assert {obj.catalog_object_id for obj in result.objects} == {"200", "201"}
    assert {(match.eso_dp_id, match.catalog_object_id) for match in result.matches} == {
        ("ESO-A", "200"),
        ("ESO-B", "200"),
        ("ESO-C", "201"),
    }
    assert all(match.separation_arcsec <= 2.0 for match in result.matches)


def test_empty_catalog_results() -> None:
    result = map_ned_results(targets(), Table())
    assert result.objects == ()
    assert result.matches == ()
    result = map_simbad_results(targets(), None)
    assert result.objects == ()
    assert result.matches == ()
