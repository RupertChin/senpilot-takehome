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


class GcsUploader(Uploader):  # pragma: no cover — exercised against live GCS at deploy
    """Prod: upload to GCS and mint a V4 signed GET URL (spec §7.7, §10).

    Signs WITHOUT a key file: on Cloud Run the runtime SA self-signs via the IAM SignBlob API
    (needs roles/iam.serviceAccountTokenCreator on itself). google-cloud-storage is sync, so the
    blocking calls run in a thread. Imports are local so the dependency isn't needed in dev.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def upload_and_sign(self, zip_path: str, key: str, ttl_hours: int) -> str:
        import asyncio

        return await asyncio.to_thread(self._upload_and_sign_sync, zip_path, key, ttl_hours)

    def _upload_and_sign_sync(self, zip_path: str, key: str, ttl_hours: int) -> str:
        from datetime import timedelta

        import google.auth
        from google.auth.transport import requests as gauth_requests
        from google.cloud import storage

        client = storage.Client(project=self.settings.gcp_project or None)
        bucket = client.bucket(self.settings.gcs_bucket)
        blob = bucket.blob(key)
        blob.upload_from_filename(zip_path, content_type="application/zip")

        # Mint a V4 signed URL via SignBlob (no private key on Cloud Run): refresh the runtime SA's
        # token and pass its email + access token so generate_signed_url uses the IAM signer.
        credentials, _ = google.auth.default()
        credentials.refresh(gauth_requests.Request())
        url = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(hours=ttl_hours),
            method="GET",
            service_account_email=getattr(credentials, "service_account_email", None),
            access_token=getattr(credentials, "token", None),
        )
        log.info("gcs_uploaded", key=key, bucket=self.settings.gcs_bucket, ttl_hours=ttl_hours)
        return url


def build_uploader(settings: Settings) -> Uploader:
    """Local → ``LocalStubUploader``; prod → ``GcsUploader`` (Stage 12)."""
    if settings.is_prod:
        return GcsUploader(settings)
    return LocalStubUploader()
