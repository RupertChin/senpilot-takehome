"""Offline tests for the packager (spec §7.7): streaming zip, de-collision, edge cases."""

from __future__ import annotations

import zipfile

import pytest

from app.config import Settings
from app.models import DownloadedDoc
from app.package.packager import _dedupe_name, package
from app.package.uploader import LocalStubUploader


async def test_local_stub_uploader_contract(tmp_path):
    src = tmp_path / "x.zip"
    src.write_bytes(b"PK\x03\x04 data")
    up = LocalStubUploader(base_dir=tmp_path / "gcs")
    url = await up.upload_and_sign(str(src), key="jobs/abc.zip", ttl_hours=72)
    assert url == "https://storage.local.stub/jobs/abc.zip?ttl=72h"
    copies = list((tmp_path / "gcs").glob("*"))
    assert len(copies) == 1 and copies[0].read_bytes() == b"PK\x03\x04 data"


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

    delivery, zip_path, _size = await package(docs, "job-x", Settings(_env_file=None))
    assert delivery == "attach"
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert sorted(names) == ["H-1.pdf", "H-1_1.pdf"]  # de-collided
    # Sources deleted as zipped.
    assert not (src / "doc0" / "H-1.pdf").exists()


async def test_package_skips_missing_source(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    docs = [DownloadedDoc(doc_no="X", filenames=["gone.pdf"], paths=[str(tmp_path / "gone.pdf")], total_bytes=0)]
    delivery, zip_path, _size = await package(docs, "job-y", Settings(_env_file=None))
    assert delivery == "attach"
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.namelist() == []  # missing source skipped, empty (valid) zip


async def test_package_over_inline_cap_returns_url_attach(tmp_path, monkeypatch):
    # Over the base64-inline cap but under max_attachment -> upload + attach BY URL.
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("ATTACH_THRESHOLD_BYTES", "10")
    f = tmp_path / "big.pdf"
    f.write_bytes(b"%PDF-1.7 " + bytes(500))
    docs = [DownloadedDoc(doc_no="B", filenames=["big.pdf"], paths=[str(f)], total_bytes=f.stat().st_size)]
    uploader = LocalStubUploader(base_dir=tmp_path / "gcs")
    delivery, url, size = await package(docs, "job-z", Settings(_env_file=None), uploader=uploader)
    assert delivery == "url_attach"
    assert url.startswith("https://storage.local.stub/jobs/job-z.zip")
    assert size > 10
    # The local zip is removed after upload (AgentMail fetches the object by URL).
    assert not (tmp_path / "job-z.zip").exists()
    assert list((tmp_path / "gcs").glob("*")) != []  # the stub "uploaded" a copy


async def test_package_over_max_returns_link(tmp_path, monkeypatch):
    # Over max_attachment (too big to ride in an email) -> upload + body link.
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("ATTACH_THRESHOLD_BYTES", "10")
    monkeypatch.setenv("MAX_ATTACHMENT_BYTES", "10")
    f = tmp_path / "huge.pdf"
    f.write_bytes(b"%PDF-1.7 " + bytes(500))
    docs = [DownloadedDoc(doc_no="H", filenames=["huge.pdf"], paths=[str(f)], total_bytes=f.stat().st_size)]
    uploader = LocalStubUploader(base_dir=tmp_path / "gcs")
    delivery, url, size = await package(docs, "job-w", Settings(_env_file=None), uploader=uploader)
    assert delivery == "link"
    assert url.startswith("https://storage.local.stub/jobs/job-w.zip")
    assert size > 10
    assert not (tmp_path / "job-w.zip").exists()
