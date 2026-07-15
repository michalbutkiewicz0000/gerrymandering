import hashlib

import gerry.law_archive as archive_module
import pytest
from gerry.law import LAW_DOCUMENTS
from gerry.law_archive import archive_law_sources, packaged_archive_path, verify_law_archive


class FakeResponse:
    def __init__(self, content: bytes):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        assert chunk_size == 1024 * 1024
        yield self.content


class FakeSession:
    def __init__(self, content=b"%PDF-1.7\nofficial-test-document"):
        self.urls = []
        self.content = content

    def get(self, url, *, stream, timeout):
        assert stream is True
        assert timeout == 180
        self.urls.append(url)
        return FakeResponse(self.content)


def test_law_archive_downloads_only_configured_eli_pdfs_and_verifies_hashes(
    tmp_path, monkeypatch
):
    session = FakeSession()
    content = b"%PDF-1.7\nofficial-test-document"
    documents = [
        {
            **item,
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
        }
        for item in LAW_DOCUMENTS
    ]
    monkeypatch.setattr(archive_module, "LAW_DOCUMENTS", documents)

    manifest = archive_law_sources(tmp_path, session=session)

    assert session.urls == [item["pdf_url"] for item in documents]
    assert [item["id"] for item in manifest["documents"]] == [
        item["id"] for item in documents
    ]
    assert verify_law_archive(tmp_path) == (True, "Zweryfikowano 3 akty prawne")
    assert not list(tmp_path.glob("*.part"))

    (tmp_path / LAW_DOCUMENTS[0]["filename"]).write_bytes(b"%PDF-tampered")
    valid, detail = verify_law_archive(tmp_path)
    assert valid is False
    assert "Niezgodna suma" in detail


def test_packaged_law_archive_is_complete_and_verified():
    assert packaged_archive_path().is_dir()
    assert verify_law_archive() == (True, "Zweryfikowano 3 akty prawne")


def test_law_archive_rejects_pdf_different_from_frozen_profile(tmp_path):
    with pytest.raises(ValueError, match="inną treść niż zamrożona"):
        archive_law_sources(tmp_path, session=FakeSession(b"%PDF-changed"))

    assert not (tmp_path / LAW_DOCUMENTS[0]["filename"]).exists()
    assert not list(tmp_path.glob("*.part"))
