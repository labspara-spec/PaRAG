"""Google Cloud Storage cloud storage provider."""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Optional

from madrag.storage.cloud.base import CloudFile, CloudStorageProvider


class GCSStorageProvider(CloudStorageProvider):
    """Google Cloud Storage backed cloud storage.

    Auth (choose one):
      - CLOUD_STORAGE_GCS_SERVICE_ACCOUNT_JSON (path to service-account JSON key)
      - Ambient ADC (gcloud auth, Workload Identity, etc.)

    Required: CLOUD_STORAGE_GCS_BUCKET
    Optional: CLOUD_STORAGE_GCS_PREFIX (default "images/")
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "images/",
        service_account_json: Optional[str] = None,
    ):
        self.bucket = bucket
        self.prefix = prefix.rstrip("/") + "/" if prefix else ""
        self._service_account_json = service_account_json

    def _get_client(self):
        try:
            from google.cloud import storage as gcs  # type: ignore
        except ImportError:
            raise RuntimeError(
                "google-cloud-storage is required for GCS storage. "
                "Install with: pip install madrag-hku[cloud-storage]"
            )
        if self._service_account_json:
            return gcs.Client.from_service_account_json(self._service_account_json)
        return gcs.Client()

    def _key(self, filename: str) -> str:
        return self.prefix + filename

    async def upload(self, filename: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        import asyncio
        key = self._key(filename)

        def _sync_upload():
            client = self._get_client()
            bucket = client.bucket(self.bucket)
            blob = bucket.blob(key)
            blob.upload_from_string(data, content_type=content_type)

        await asyncio.get_event_loop().run_in_executor(None, _sync_upload)
        return f"gs://{self.bucket}/{key}"

    async def download(self, cloud_path: str) -> bytes:
        import asyncio
        key = cloud_path.removeprefix(f"gs://{self.bucket}/") if cloud_path.startswith("gs://") else cloud_path

        def _sync_download():
            client = self._get_client()
            bucket = client.bucket(self.bucket)
            blob = bucket.blob(key)
            return blob.download_as_bytes()

        return await asyncio.get_event_loop().run_in_executor(None, _sync_download)

    async def delete(self, cloud_path: str) -> None:
        import asyncio
        key = cloud_path.removeprefix(f"gs://{self.bucket}/") if cloud_path.startswith("gs://") else cloud_path

        def _sync_delete():
            client = self._get_client()
            bucket = client.bucket(self.bucket)
            blob = bucket.blob(key)
            blob.delete()

        await asyncio.get_event_loop().run_in_executor(None, _sync_delete)

    async def list_files(self, prefix: str = "") -> list[CloudFile]:
        import asyncio
        list_prefix = self.prefix + prefix

        def _sync_list():
            client = self._get_client()
            bucket = client.bucket(self.bucket)
            blobs = list(bucket.list_blobs(prefix=list_prefix))
            return blobs

        blobs = await asyncio.get_event_loop().run_in_executor(None, _sync_list)
        files: list[CloudFile] = []
        for blob in blobs:
            name = blob.name[len(self.prefix):] if blob.name.startswith(self.prefix) else blob.name
            files.append(CloudFile(
                name=name,
                cloud_path=f"gs://{self.bucket}/{blob.name}",
                size=blob.size or 0,
                last_modified=blob.updated,
                content_type=blob.content_type or "application/octet-stream",
            ))
        return files

    async def exists(self, cloud_path: str) -> bool:
        import asyncio
        key = cloud_path.removeprefix(f"gs://{self.bucket}/") if cloud_path.startswith("gs://") else cloud_path

        def _sync_exists():
            try:
                client = self._get_client()
                bucket = client.bucket(self.bucket)
                blob = bucket.blob(key)
                return blob.exists()
            except Exception:
                return False

        return await asyncio.get_event_loop().run_in_executor(None, _sync_exists)

    async def get_image_url(self, cloud_path: str, _expiry_seconds: int = 3600) -> str:
        import asyncio
        key = cloud_path.removeprefix(f"gs://{self.bucket}/") if cloud_path.startswith("gs://") else cloud_path

        def _sync_signed_url():
            client = self._get_client()
            bucket = client.bucket(self.bucket)
            blob = bucket.blob(key)
            return blob.generate_signed_url(expiration=timedelta(seconds=_expiry_seconds), version="v4")

        return await asyncio.get_event_loop().run_in_executor(None, _sync_signed_url)

    @classmethod
    def from_env(cls) -> "GCSStorageProvider":
        bucket = os.environ.get("CLOUD_STORAGE_GCS_BUCKET", "")
        if not bucket:
            raise ValueError("CLOUD_STORAGE_GCS_BUCKET must be set for GCS storage provider")
        return cls(
            bucket=bucket,
            prefix=os.environ.get("CLOUD_STORAGE_GCS_PREFIX", "images/"),
            service_account_json=os.environ.get("CLOUD_STORAGE_GCS_SERVICE_ACCOUNT_JSON"),
        )
