from __future__ import annotations

from astropy.table import Table

from eso_object_types.models import SearchTarget
from eso_object_types.services import (
    build_eso_query,
    build_simbad_alias_query,
    build_simbad_query,
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

    names = build_simbad_query(by_name=True)
    assert "i.id = u.target_name" in names
    assert "i.oidref = b.oid" in names
    assert "CONTAINS" not in names
    assert "separation_arcsec" in names

    aliases = build_simbad_alias_query()
    assert "TAP_UPLOAD.objects" in aliases
    assert "ident" in aliases


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
    fallback = map_simbad_results(
        targets(), table, match_method="target_name_fallback"
    )
    assert all(match.match_method == "target_name_fallback" for match in fallback.matches)


def test_empty_catalog_results() -> None:
    for table in (None, Table()):
        result = map_simbad_results(targets(), table)
        assert result.objects == ()
        assert result.matches == ()


def test_name_lookup_uploads_literal_identifiers() -> None:
    from types import SimpleNamespace
    from eso_object_types.services import SimbadClient

    calls = []

    def query_tap(query, **kwargs):
        calls.append((query, kwargs))
        return None

    client = SimbadClient.__new__(SimbadClient)
    client.client = SimpleNamespace(hardlimit=200000, query_tap=query_tap)
    result = client.query_name_batch([
        SearchTarget("name-1", 10.0, -20.0, 1 / 3600, ("ESO-A",), "A' name_%")
    ])
    assert result.objects == ()
    query, kwargs = calls[0]
    assert "A' name_%" not in query
    assert kwargs["targets"]["target_name"][0] == "A' name_%"
    assert kwargs["targets"]["ra_deg"][0] == 10.0
