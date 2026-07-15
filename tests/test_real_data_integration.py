from pathlib import Path

import pytest

from gerry.real_smoke import run_real_smoke


SOURCE = Path("../mapa_obwodow")
pytestmark = pytest.mark.skipif(
    not (SOURCE / "data/raw/prg/020302.parquet").is_file(),
    reason="wymaga lokalnego projektu mapa_obwodow z rzeczywistymi danymi",
)


def test_real_municipality_end_to_end(tmp_path):
    result = run_real_smoke(SOURCE, tmp_path, "020302")

    assert result["precincts"] == result["expected_precincts"] == 4
    assert result["assignment_rate"] == 1.0
    assert result["graph_errors"] == []
    assert result["scenario_units"] == 4
    assert result["committees"] == 12
    assert result["target"] == "KOMITET WYBORCZY PRAWO I SPRAWIEDLIWOŚĆ"
    assert result["status"] == "OPTIMAL"
    assert result["certificate_verified"] is True
    assert Path(result["export"]).is_file()
