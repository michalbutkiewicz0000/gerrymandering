import json
import zipfile

from gerry.elections import scenario_from_pkw


def test_pkw_scenario_import_attaches_special_votes(tmp_path):
    results = tmp_path / "results.csv"
    results.write_text(
        "TERYT,Numer obwodu,Uprawnieni,A,B\n"
        "020101,1,100,60,30\n"
        "020101,9,20,10,5\n",
        encoding="utf-8",
    )
    attachments = tmp_path / "attachments.json"
    attachments.write_text(
        json.dumps([{"special_key": "020101_9", "host_key": "020101_1"}]),
        encoding="utf-8",
    )
    scenario = scenario_from_pkw(results, "history", attachments_path=attachments)
    assert scenario.votes_by_unit == {"020101_1": {"A": 70, "B": 35}}
    assert scenario.eligible_by_unit == {"020101_1": 120}


def test_real_pkw_header_variants_can_be_read_from_zip(tmp_path):
    archive = tmp_path / "results.zip"
    csv = (
        "Nr komisji;TERYT Gminy;Liczba wyborców uprawnionych do głosowania;A;B\n"
        "1;20101;100;60;30\n"
    )
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("wyniki.csv", csv)
    scenario = scenario_from_pkw(archive, "sejm", vote_columns=["A", "B"])
    assert scenario.votes_by_unit == {"020101_1": {"A": 60, "B": 30}}
    assert scenario.eligible_by_unit == {"020101_1": 100}


def test_empty_special_attachment_report_is_valid(tmp_path):
    results = tmp_path / "results.csv"
    results.write_text(
        "TERYT,Numer obwodu,Uprawnieni,A\n020302,1,100,60\n",
        encoding="utf-8",
    )
    attachments = tmp_path / "attachments.json"
    attachments.write_text("[]", encoding="utf-8")

    scenario = scenario_from_pkw(
        results, "without-special", attachments_path=attachments
    )

    assert scenario.votes_by_unit == {"020302_1": {"A": 60}}


def test_official_committee_headers_exclude_turnout_statistics(tmp_path):
    archive = tmp_path / "results.zip"
    csv = (
        "Nr komisji;TERYT Gminy;Liczba wyborców uprawnionych do głosowania;"
        "Komisja otrzymała kart do głosowania;KOMITET WYBORCZY ALFA;"
        "KOALICYJNY KOMITET WYBORCZY BETA\n"
        "1;020302;100;105;60;30\n"
    )
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("wyniki.csv", csv)

    scenario = scenario_from_pkw(archive, "official")

    assert scenario.votes_by_unit == {
        "020302_1": {
            "KOMITET WYBORCZY ALFA": 60,
            "KOALICYJNY KOMITET WYBORCZY BETA": 30,
        }
    }
