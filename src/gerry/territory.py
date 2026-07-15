from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=1)
def teryt_names() -> dict[str, dict[str, str]]:
    """Frozen TERYT dictionary of województwo, powiat and gmina names.

    Keyed by code length: 2-digit województwa, 4-digit powiaty, 6-digit gminy.
    """
    text = files("gerry").joinpath("resources/teryt_names.json").read_text(encoding="utf-8")
    return json.loads(text)


def unit_options() -> dict[str, list[dict[str, str]]]:
    """Cascade options for the wizard: each unit with its name and parent code.

    A powiat's parent is its 2-digit województwo, a gmina's its 4-digit powiat, so
    the frontend narrows województwo → powiat → gmina purely by prefix.
    """
    names = teryt_names()
    return {
        "wojewodztwa": [
            {"code": code, "name": name}
            for code, name in names["wojewodztwa"].items()
        ],
        "powiaty": [
            {"code": code, "name": name, "parent": code[:2]}
            for code, name in names["powiaty"].items()
        ],
        "gminy": [
            {"code": code, "name": name, "parent": code[:4]}
            for code, name in names["gminy"].items()
        ],
    }
