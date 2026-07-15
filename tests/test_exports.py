import json

import pytest

from gerry.exports import export_run
from gerry.solver import ExactEnumerator

from test_solver import small_request


def test_export_csv_geojson_and_html(tmp_path):
    request = small_request()
    request.geometry_by_node = {
        node: {"type": "Polygon", "coordinates": [[[i, 0], [i + 1, 0], [i + 1, 1], [i, 1], [i, 0]]]}
        for i, node in enumerate(request.nodes)
    }
    run = ExactEnumerator(tmp_path / "certs").solve(request)
    csv_path = export_run(run, tmp_path / "plan.csv", "csv")
    geojson_path = export_run(run, tmp_path / "plan.geojson", "geojson")
    html_path = export_run(run, tmp_path / "report.html", "html")
    assert "node,district" in csv_path.read_text()
    assert len(json.loads(geojson_path.read_text())["features"]) == 4
    assert "zweryfikowany" in html_path.read_text()
    assert not list(tmp_path.glob(".*.part.*"))


def test_failed_export_keeps_previous_file_and_removes_temporary(tmp_path):
    run = ExactEnumerator(tmp_path / "certs").solve(small_request())
    output = tmp_path / "plan.gpkg"
    output.write_bytes(b"previous-complete-export")

    with pytest.raises(ValueError, match="wymaga geometry_by_node"):
        export_run(run, output, "gpkg")

    assert output.read_bytes() == b"previous-complete-export"
    assert not (tmp_path / ".plan.part.gpkg").exists()
