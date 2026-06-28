"""Large-file uploader abstraction (spec §7.7).

The packager's link branch uploads an oversized zip and mints a time-limited download URL. Two
implementations behind one interface, selected by environment:
  - ``LocalStubUploader`` (local): copies the zip to a local stub dir and returns a fake URL, so
    the link branch is fully exercisable with no GCS account.
  - ``GcsUploader`` (prod, wired in Stage 12): uploads to GCS and mints a V4 signed GET URL.
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from pathlib import Path

from app.config import Settings
from app.observability import get_logger

log = get_logger(__name__)


class Uploader(ABC):
    @abstractmethod
    async def upload_and_sign(self, zip_path: str, key: str, ttl_hours: int) -> str:
        """Upload ``zip_path`` under ``key`` and return a download URL valid for ``ttl_hours``."""
        ...


class LocalStubUploader(Uploader):
    """Local substitute: copies the zip to a stub dir and returns a placeholder URL.

    Lets the oversized-zip link path be tested end to end without GCS. Not a real signed URL.
    """

    def __init__(self, base_dir: str | Path = "outbox/gcs-stub") -> None:
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)

    async def upload_and_sign(self, zip_path: str, key: str, ttl_hours: int) -> str:
        dest = self.base / key.replace("/", "_")
        shutil.copyfile(zip_path, dest)
        url = f"https://storage.local.stub/{key}?ttl={ttl_hours}h"
        log.info("stub_uploaded", key=key, dest=str(dest), ttl_hours=ttl_hours)
        return url


class GcsUploader(Uploader):  # pragma: no cover — wired + tested in Stage 12 (needs GCP)
    """Prod: upload to GCS and mint a V4 signed GET URL (spec §7.7, §10).

    Requires the runtime SA to self-sign via the IAM SignBlob API (roles/iam.serviceAccountTokenCreator
    on itself) so no key file is needed. Built lazily so google-cloud-storage isn't imported locally.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def upload_and_sign(self, zip_path: str, key: str, ttl_hours: int) -> str:
        raise NotImplementedError(
            "GcsUploader is wired in Stage 12 (deploy); local uses LocalStubUploader"
        )


def build_uploader(settings: Settings) -> Uploader:
    """Local → ``LocalStubUploader``; prod → ``GcsUploader`` (Stage 12)."""
    if settings.is_prod:
        return GcsUploader(settings)
    return LocalStubUploader()
