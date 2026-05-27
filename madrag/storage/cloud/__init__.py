"""Cloud storage provider factory."""

from __future__ import annotations

import os
from typing import Optional

from madrag.storage.cloud.base import CloudFile, CloudStorageProvider

__all__ = ["CloudFile", "CloudStorageProvider", "get_cloud_provider"]


def get_cloud_provider(args=None) -> Optional[CloudStorageProvider]:
    """Return the configured CloudStorageProvider, or None for local mode.

    Reads CLOUD_STORAGE_PROVIDER from args (if provided) or environment.
    """
    provider_name = (
        getattr(args, "cloud_storage_provider", None)
        or os.environ.get("CLOUD_STORAGE_PROVIDER", "")
    )
    if not provider_name:
        return None

    provider_name = provider_name.strip().lower()

    if provider_name == "s3":
        from madrag.storage.cloud.s3 import S3StorageProvider
        return S3StorageProvider.from_env()

    if provider_name == "azure_blob":
        from madrag.storage.cloud.azure_blob import AzureBlobStorageProvider
        return AzureBlobStorageProvider.from_env()

    if provider_name in ("sharepoint", "onedrive"):
        from madrag.storage.cloud.graph import GraphStorageProvider
        return GraphStorageProvider.from_env()

    if provider_name == "google_drive":
        from madrag.storage.cloud.google_drive import GoogleDriveStorageProvider
        return GoogleDriveStorageProvider.from_env()

    if provider_name == "gcs":
        from madrag.storage.cloud.gcs import GCSStorageProvider
        return GCSStorageProvider.from_env()

    raise ValueError(
        f"Unknown CLOUD_STORAGE_PROVIDER '{provider_name}'. "
        "Supported: s3, azure_blob, sharepoint, onedrive, google_drive, gcs"
    )
