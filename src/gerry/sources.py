from __future__ import annotations

import io
import re
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests


PRG_WFS_URL = "https://mapy.geoportal.gov.pl/wss/ext/KrajowaIntegracjaNumeracjiAdresowej"
PRG_LAYER = "ms:prg-adresy"
PRG_BOUNDARIES_WFS_URL = "https://mapy.geoportal.gov.pl/wss/service/PZGIK/PRG/WFS/AdministrativeBoundaries"
PAGE_SIZE = 10_000

UNIT_LEVELS = ("precinct", "gmina", "powiat")


def node_key_for_teryt(teryt: str, level: str, keep_gmina: frozenset[str] = frozenset()) -> str:
    """Node key for a gmina TERYT at ``level``.

    A gmina node is the 6-digit TERYT, a powiat node its first four digits.
    ``keep_gmina`` holds gmina TERYT codes that stay gmina-grained even under the
    powiat level — Warsaw districts the Senate profile is allowed to split.
    """
    if level == "gmina":
        return teryt
    if level == "powiat":
        return teryt if teryt in keep_gmina else teryt[:4]
    raise ValueError(f"level {level!r} has no teryt-derived node key; expected 'gmina' or 'powiat'")


def normalize_teryt(value) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip().strip('"').replace(".0", "")
    return text.zfill(6) if text else ""


def normalize_precinct(value) -> int | None:
    if value is None or pd.isna(value) or str(value).strip() == "":
        return None
    return int(float(str(value).replace(" ", "").replace(",", ".")))


def load_registry(path: Path) -> pd.DataFrame:
    frame = pd.read_excel(path, dtype=str) if path.suffix.lower() in {".xlsx", ".xls"} else pd.read_csv(path, dtype=str, sep=None, engine="python")
    aliases = {
        "TERYT gminy": "teryt", "Teryt gminy": "teryt", "teryt": "teryt",
        "Numer": "precinct", "Nr obwodu": "precinct", "Numer obwodu": "precinct",
        "Opis granic": "description", "Typ obszaru": "area_type",
        "Pełna siedziba": "commission", "Siedziba": "commission",
        "Typ obwodu": "precinct_type", "Rodzaj obwodu": "precinct_type",
        "Wyborcy": "eligible", "Liczba wyborców": "eligible",
        "Mieszkańcy": "population", "Liczba mieszkańców": "population",
        "Gmina": "gmina", "Powiat": "powiat", "Województwo": "wojewodztwo",
    }
    frame = frame.rename(columns={column: aliases[column.strip()] for column in frame.columns if column.strip() in aliases})
    if frame.columns.duplicated().any():
        coalesced = {}
        for column in dict.fromkeys(frame.columns):
            candidates = frame.loc[:, frame.columns == column]
            coalesced[column] = candidates.replace("", pd.NA).bfill(axis=1).iloc[:, 0]
        frame = pd.DataFrame(coalesced)
    required = {"teryt", "precinct", "description", "area_type"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Rejestr nie zawiera kolumn: {', '.join(sorted(missing))}")
    frame["teryt"] = frame["teryt"].map(normalize_teryt)
    frame["precinct"] = frame["precinct"].map(normalize_precinct).astype("Int64")
    frame = frame.dropna(subset=["precinct"])
    frame["precinct"] = frame["precinct"].astype(int)
    for column in (
        "commission", "precinct_type", "eligible", "population", "gmina", "powiat", "wojewodztwo"
    ):
        if column not in frame:
            frame[column] = ""
    for column in ("eligible", "population"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("Int64")
    normalized_type = frame["precinct_type"].fillna("").str.lower()
    frame["special"] = normalized_type.str.contains("odrębn|odrebn|szpital|zakład|zaklad|areszt|dom pomocy")
    return frame


class PrgClient:
    def __init__(self, session: requests.Session | None = None, page_size: int = PAGE_SIZE):
        self.session = session or requests.Session()
        self.page_size = page_size

    @staticmethod
    def filter_xml(teryt: str) -> str:
        return (
            '<Filter xmlns="http://www.opengis.net/fes/2.0">'
            "<PropertyIsEqualTo><ValueReference>teryt</ValueReference>"
            f"<Literal>{teryt}</Literal></PropertyIsEqualTo></Filter>"
        )

    def _params(self, teryt: str, **extra) -> dict:
        return {
            "Service": "WFS", "Request": "GetFeature", "TypeName": PRG_LAYER,
            "Version": "2.0.0", "FILTER": self.filter_xml(teryt), **extra,
        }

    def count(self, teryt: str) -> int:
        response = self.session.get(PRG_WFS_URL, params=self._params(teryt, resultType="hits"), timeout=60)
        response.raise_for_status()
        match = re.search(r'numberMatched="(\d+)"', response.text)
        return int(match.group(1)) if match else 0

    def fetch(self, teryt: str, cache_dir: Path, retries: int = 3) -> gpd.GeoDataFrame:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{teryt}.parquet"
        if cache_file.exists():
            frame = pd.read_parquet(cache_file)
            return gpd.GeoDataFrame(frame, geometry=gpd.points_from_xy(frame.lon, frame.lat), crs=4326)
        total = self.count(teryt)
        if not total:
            raise ValueError(f"PRG nie zwrócił punktów dla TERYT {teryt}")
        pages = []
        for start in range(0, total, self.page_size):
            for attempt in range(retries + 1):
                try:
                    response = self.session.get(
                        PRG_WFS_URL,
                        params=self._params(teryt, count=self.page_size, STARTINDEX=start),
                        timeout=120,
                    )
                    response.raise_for_status()
                    pages.append(gpd.read_file(io.BytesIO(response.content)))
                    break
                except Exception:
                    if attempt == retries:
                        raise
                    time.sleep(2 ** attempt)
        raw = gpd.GeoDataFrame(pd.concat(pages, ignore_index=True), crs=pages[0].crs).to_crs(4326)
        rows = []
        for row in raw.itertuples(index=False):
            raw_number = str(getattr(row, "numer", "") or "")
            match = re.match(r"\s*(\d+)", raw_number)
            rows.append({
                "street": getattr(row, "ulica", "") or "",
                "number": int(match.group(1)) if match else None,
                "housenumber": raw_number,
                "miejscowosc": getattr(row, "miejscowosc", "") or "",
                "lat": row.geometry.y, "lon": row.geometry.x,
            })
        frame = pd.DataFrame(rows)
        temporary = cache_file.with_name(f"{cache_file.stem}.tmp{cache_file.suffix}")
        frame.to_parquet(temporary, index=False)
        temporary.replace(cache_file)
        return gpd.GeoDataFrame(frame, geometry=gpd.points_from_xy(frame.lon, frame.lat), crs=4326)


class AdministrativeBoundaryClient:
    LAYERS = {
        "gminy": "ms:A03_Granice_gmin",
        "rejony_statystyczne": "ms:R01_Granice_rejonow_statystycznych",
        "obwody_spisowe": "ms:R02_Granice_obwodow_spisowych",
    }

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()

    def fetch_layer(self, level: str, cache_dir: Path, *, force: bool = False) -> gpd.GeoDataFrame:
        if level not in self.LAYERS:
            raise ValueError(f"Nieznany poziom PRG/BREC: {level}")
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"{level}.parquet"
        if path.exists() and not force:
            return gpd.read_parquet(path)
        response = self.session.get(
            PRG_BOUNDARIES_WFS_URL,
            params={
                "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature",
                "TYPENAMES": self.LAYERS[level], "SRSNAME": "EPSG:2180",
            },
            timeout=300,
        )
        response.raise_for_status()
        frame = gpd.read_file(io.BytesIO(response.content)).to_crs(2180)
        if level == "gminy":
            frame["teryt"] = frame["JPT_KOD_JE"].astype(str).str[:6]
            frame = frame[["teryt", "JPT_NAZWA_", "geometry"]].rename(columns={"JPT_NAZWA_": "name"})
        temporary = path.with_name(f"{path.stem}.tmp{path.suffix}")
        frame.to_parquet(temporary, index=False)
        temporary.replace(path)
        return frame

    def fetch_gminy(self, cache_dir: Path, *, force: bool = False) -> gpd.GeoDataFrame:
        return self.fetch_layer("gminy", cache_dir, force=force)
