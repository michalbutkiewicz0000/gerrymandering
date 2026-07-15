from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_frontend_map_is_self_contained_and_offline() -> None:
    index = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")

    assert '<svg id="map"' in index
    assert "renderGeoJson" in script
    assert "maplibre" not in (index + script).lower()
    assert "openstreetmap" not in (index + script).lower()
    assert "https://" not in (index + script).lower()


def test_frontend_form_covers_every_district_rule() -> None:
    index = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    controls = {
        "district-count": "district_count",
        "seats": "seats_per_district",
        "population-tolerance": "population_tolerance",
        "edge-kinds": "allowed_edge_kinds",
        "max-cut-border": "max_cut_border_m",
        "indivisible-parent-level": "indivisible_parent_level",
    }

    for control, field in controls.items():
        assert f'id="{control}"' in index
        assert field in script


def test_frontend_area_selection_filters_all_node_scoped_inputs() -> None:
    index = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")

    assert 'id="select-all"' in index
    assert "/precincts" in script
    assert "toggleNode" in script
    for field in (
        "nodes",
        "edges",
        "votes_by_unit",
        "eligible_by_unit",
        "population_by_unit",
        "geometry_by_node",
        "base_assignment",
        "parent_by_node",
        "container_by_node",
    ):
        assert field in script
