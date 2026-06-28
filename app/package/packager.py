"""Packager (spec §7.7) — Stage 6: streaming on-disk zip + attach branch.

Builds the zip incrementally on disk (deleting each source temp file as it's added, to cap peak
disk/RAM — files can be 48MB+), de-colliding duplicate filenames. Returns ``("attach", zip_path)``.

The threshold gate (raw zip ≤ attach_threshold → attach; else GCS upload + V4 signed URL → link)
is wired in Stage 7; the size is computed here and logged so the branch slots in cleanly.
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

from app.config import Settings
from app.models import DownloadedDoc
from app.observability import get_logger
from app.package.uploader import Uploader, build_uploader

log = get_logger(__name__)


def _dedupe_name(name: str, used: set[str]) -> str:
    """Return a zip-internal name that doesn't collide with one already used."""
    if name not in used:
        used.add(name)
        return name
    stem, ext = os.path.splitext(name)
    i = 1
    while f"{stem}_{i}{ext}" in used:
        i += 1
    final = f"{stem}_{i}{ext}"
    used.add(final)
    return final


async def package(
    docs: list[DownloadedDoc],
    job_id: str,
    settings: Settings,
    uploader: Uploader | None = None,
) -> tuple[str, str, int]:
    """Build the zip and decide delivery (spec §7.7).

    Returns ``(delivery, payload, raw_zip_bytes)``: ``("attach", zip_path, size)`` when the raw zip
    is ≤ the (base64-aware) threshold; otherwise ``("link", signed_url, size)`` after uploading —
    the caller uses ``size`` for the §5 "It's {size}MB — too large to attach" wording.
    """
    out_dir = Path(os.environ.get("TMPDIR", "/tmp"))
    zip_path = out_dir / f"{job_id}.zip"
    used: set[str] = set()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for doc in docs:
            for src in doc.paths:
                src_path = Path(src)
                if not src_path.exists():
                    log.warning("package_source_missing", path=src)
                    continue
                arcname = _dedupe_name(src_path.name, used)
                zf.write(src_path, arcname)
                # Delete the source as we go to cap peak disk usage.
                try:
                    src_path.unlink()
                except OSError:
                    pass

    size = zip_path.stat().st_size
    log.info(
        "packaged",
        job_id=job_id,
        files=len(used),
        zip_bytes=size,
        threshold=settings.attach_threshold_bytes,
    )

    if size <= settings.attach_threshold_bytes:
        return ("attach", str(zip_path), size)

    # Oversized: upload + mint a time-limited link (states the size reason in the reply).
    uploader = uploader or build_uploader(settings)
    url = await uploader.upload_and_sign(
        str(zip_path), key=f"jobs/{job_id}.zip", ttl_hours=settings.signed_url_ttl_hours
    )
    # The local zip is now redundant (the user fetches it from the link); remove it.
    try:
        zip_path.unlink()
    except OSError:
        pass
    log.info("packaged_link", job_id=job_id, zip_bytes=size)
    return ("link", url, size)
