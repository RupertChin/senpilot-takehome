"""Offline tests for the packager (spec §7.7): streaming zip, de-collision, edge cases."""

from __future__ import annotations

import zipfile

import pytest

from app.config import Settings
from app.models import DownloadedDoc
from app.package.packager import _dedupe_name, package


def test_dedupe_name_collisions():
    used: set[str] = set()
    assert _dedupe_name("a.pdf", used) == "a.pdf"
    assert _dedupe_name("a.pdf", used) == "a_1.pdf"
    assert _dedupe_name("a.pdf", used) == "a_2.pdf"
    assert _dedupe_name("b.pdf", used) == "b.pdf"


async def test_package_attaches_and_dedupes(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    src = tmp_path / "src"
    src.mkdir()
    docs = []
    # Two documents whose files share the same basename -> must de-collide in the zip.
    for i in range(2):
        d = src / f"doc{i}"
        d.mkdir()
        f = d / "H-1.pdf"
        f.write_bytes(b"%PDF-1.7 " + bytes(50))
        docs.append(DownloadedDoc(doc_no=f"H-1-{i}", filenames=["H-1.pdf"], paths=[str(f)], total_bytes=f.stat().st_size))

    delivery, zip_path = await package(docs, "job-x", Settings(_env_file=None))
    assert delivery == "attach"
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert sorted(names) == ["H-1.pdf", "H-1_1.pdf"]  # de-collided
    # Sources deleted as zipped.
    assert not (src / "doc0" / "H-1.pdf").exists()


async def test_package_skips_missing_source(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    docs = [DownloadedDoc(doc_no="X", filenames=["gone.pdf"], paths=[str(tmp_path / "gone.pdf")], total_bytes=0)]
    delivery, zip_path = await package(docs, "job-y", Settings(_env_file=None))
    assert delivery == "attach"
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.namelist() == []  # missing source skipped, empty (valid) zip


async def test_package_over_threshold_raises_until_stage7(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("ATTACH_THRESHOLD_BYTES", "10")  # force the link branch
    f = tmp_path / "big.pdf"
    f.write_bytes(b"%PDF-1.7 " + bytes(500))
    docs = [DownloadedDoc(doc_no="B", filenames=["big.pdf"], paths=[str(f)], total_bytes=f.stat().st_size)]
    with pytest.raises(NotImplementedError):
        await package(docs, "job-z", Settings(_env_file=None))
