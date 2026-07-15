from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlparse

import requests

from .law import LAW_DOCUMENTS


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_pdf(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(5) == b"%PDF-"


def archive_law_sources(
    output: Path,
    *,
    session: requests.Session | None = None,
) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    client = session or requests.Session()
    archived = []
    for document in LAW_DOCUMENTS:
        url = str(document["pdf_url"])
        if urlparse(url).scheme != "https" or urlparse(url).hostname != "eli.gov.pl":
            raise ValueError(f"Niedozwolone źródło aktu: {url}")
        destination = output / str(document["filename"])
        temporary = destination.with_suffix(destination.suffix + ".part")
        with client.get(url, stream=True, timeout=180) as response:
            response.raise_for_status()
            with temporary.open("wb") as handle:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        handle.write(chunk)
        if not _is_pdf(temporary):
            temporary.unlink(missing_ok=True)
            raise ValueError(f"ELI nie zwróciło pliku PDF dla {document['id']}")
        digest = _sha256(temporary)
        size = temporary.stat().st_size
        if digest != document["sha256"] or size != document["bytes"]:
            temporary.unlink(missing_ok=True)
            raise ValueError(
                f"ELI zwróciło inną treść niż zamrożona dla {document['id']}"
            )
        temporary.replace(destination)
        archived.append(dict(document))
    manifest = {"schema_version": 1, "documents": archived}
    manifest_path = output / "manifest.json"
    temporary_manifest = manifest_path.with_suffix(".json.part")
    temporary_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary_manifest.replace(manifest_path)
    return manifest


def packaged_archive_path() -> Path:
    return Path(str(files("gerry").joinpath("resources/legal")))


def verify_law_archive(root: Path | None = None) -> tuple[bool, str]:
    root = root or packaged_archive_path()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return False, "Brak manifestu archiwum prawa"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        records = manifest["documents"]
    except (KeyError, json.JSONDecodeError, TypeError) as exc:
        return False, f"Niepoprawny manifest archiwum prawa: {exc}"
    expected = {str(item["id"]): item for item in LAW_DOCUMENTS}
    if {str(item.get("id")) for item in records} != set(expected):
        return False, "Manifest nie zawiera dokładnie aktów z profilu prawnego"
    for record in records:
        identifier = str(record["id"])
        source = expected[identifier]
        if any(
            record.get(field) != source[field]
            for field in ("page_url", "pdf_url", "filename", "sha256", "bytes")
        ):
            return False, f"Źródło {identifier} nie odpowiada profilowi"
        path = root / str(record["filename"])
        if not path.is_file() or not _is_pdf(path):
            return False, f"Brak poprawnego PDF: {record['filename']}"
        if _sha256(path) != record.get("sha256") or path.stat().st_size != record.get("bytes"):
            return False, f"Niezgodna suma lub rozmiar: {record['filename']}"
    return True, f"Zweryfikowano {len(records)} akty prawne"
