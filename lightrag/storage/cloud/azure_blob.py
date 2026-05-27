"""Azure Blob Storage cloud storage provider."""

from __future__ import annotations

import os
from typing import Optional

from lightrag.storage.cloud.base import CloudFile, CloudStorageProvider


class AzureBlobStorageProvider(CloudStorageProvider):
    """Azure Blob Storage backed cloud storage.

    Auth (choose one):
      - CLOUD_STORAGE_AZURE_CONNECTION_STRING
      - CLOUD_STORAGE_AZURE_ACCOUNT_NAME alone (uses DefaultAzureCredential / keyless)

    Required: CLOUD_STORAGE_AZURE_CONTAINER
    """

    def __init__(
        self,
        container: str,
        connection_string: Optional[str] = None,
        account_name: Optional[str] = None,
    ):
        self.container = container
        self._connection_string = connection_string
        self._account_name = account_name

    def _get_client(self):
        try:
            from azure.storage.blob.aio import BlobServiceClient
        except ImportError:
            raise RuntimeError(
                "azure-storage-blob is required for Azure Blob storage. "
                "Install with: pip install lightrag-hku[cloud-storage]"
            )

        if self._connection_string:
            return BlobServiceClient.from_connection_string(self._connection_string)

        if self._account_name:
            try:
                from azure.identity.aio import DefaultAzureCredential
            except ImportError:
                raise RuntimeError(
                    "azure-identity is required for keyless Azure auth. "
                    "Install with: pip install lightrag-hku[cloud-storage]"
                )
            credential = DefaultAzureCredential()
            url = f"https://{self._account_name}.blob.core.windows.net"
            return BlobServiceClient(account_url=url, credential=credential)

        raise ValueError(
            "Either CLOUD_STORAGE_AZURE_CONNECTION_STRING or "
            "CLOUD_STORAGE_AZURE_ACCOUNT_NAME must be set"
        )

    async def upload(self, filename: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        async with self._get_client() as service_client:
            container_client = service_client.get_container_client(self.container)
            blob_client = container_client.get_blob_client(filename)
            await blob_client.upload_blob(data, overwrite=True, content_settings=None)
        return filename

    async def download(self, cloud_path: str) -> bytes:
        async with self._get_client() as service_client:
            container_client = service_client.get_container_client(self.container)
            blob_client = container_client.get_blob_client(cloud_path)
            stream = await blob_client.download_blob()
            return await stream.readall()

    async def delete(self, cloud_path: str) -> None:
        async with self._get_client() as service_client:
            container_client = service_client.get_container_client(self.container)
            blob_client = container_client.get_blob_client(cloud_path)
            await blob_client.delete_blob()

    async def list_files(self, prefix: str = "") -> list[CloudFile]:
        files: list[CloudFile] = []
        async with self._get_client() as service_client:
            container_client = service_client.get_container_client(self.container)
            async for blob in container_client.list_blobs(name_starts_with=prefix):
                files.append(CloudFile(
                    name=blob.name,
                    cloud_path=blob.name,
                    size=blob.size or 0,
                    last_modified=blob.last_modified,
                    content_type=blob.content_settings.content_type if blob.content_settings else "application/octet-stream",
                ))
        return files

    async def exists(self, cloud_path: str) -> bool:
        try:
            async with self._get_client() as service_client:
                container_client = service_client.get_container_client(self.container)
                blob_client = container_client.get_blob_client(cloud_path)
                return await blob_client.exists()
        except Exception:
            return False

    async def get_image_url(self, cloud_path: str, _expiry_seconds: int = 3600) -> str:
        from datetime import timedelta
        try:
            from azure.storage.blob import generate_blob_sas, BlobSasPermissions  # type: ignore
            from datetime import datetime, timezone
        except ImportError:
            raise RuntimeError(
                "azure-storage-blob is required for Azure signed URLs. "
                "Install with: pip install lightrag-hku[cloud-storage]"
            )
        if self._connection_string:
            from azure.storage.blob import BlobServiceClient as _SyncBSC  # type: ignore
            svc = _SyncBSC.from_connection_string(self._connection_string)
            account_name = svc.account_name
            account_key = svc.credential.account_key if svc.credential else None
        else:
            account_name = self._account_name
            account_key = None

        if not account_key:
            # Keyless — fall back to returning the plain cloud path;
            # caller must handle serving via the async client directly.
            return cloud_path

        sas_token = generate_blob_sas(
            account_name=account_name,
            container_name=self.container,
            blob_name=cloud_path,
            account_key=account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(seconds=_expiry_seconds),
        )
        return f"https://{account_name}.blob.core.windows.net/{self.container}/{cloud_path}?{sas_token}"

    @classmethod
    def from_env(cls) -> "AzureBlobStorageProvider":
        container = os.environ.get("CLOUD_STORAGE_AZURE_CONTAINER", "")
        if not container:
            raise ValueError("CLOUD_STORAGE_AZURE_CONTAINER must be set for Azure Blob storage provider")
        return cls(
            container=container,
            connection_string=os.environ.get("CLOUD_STORAGE_AZURE_CONNECTION_STRING"),
            account_name=os.environ.get("CLOUD_STORAGE_AZURE_ACCOUNT_NAME"),
        )
