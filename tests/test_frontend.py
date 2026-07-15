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
