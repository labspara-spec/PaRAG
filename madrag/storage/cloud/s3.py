"""AWS S3 cloud storage provider."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from madrag.storage.cloud.base import CloudFile, CloudStorageProvider


class S3StorageProvider(CloudStorageProvider):
    """AWS S3 backed cloud storage.

    Required env vars: CLOUD_STORAGE_S3_BUCKET, AWS_REGION
    Auth: AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY, or ambient credentials.
    Optional: CLOUD_STORAGE_S3_PREFIX (default "documents/")
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "documents/",
        region: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        session_token: Optional[str] = None,
    ):
        self.bucket = bucket
        self.prefix = prefix.rstrip("/") + "/" if prefix else ""
        self._region = region
        self._access_key = access_key
        self._secret_key = secret_key
        self._session_token = session_token

    def _client(self):
        try:
            import aioboto3
        except ImportError:
            raise RuntimeError(
                "aioboto3 is required for S3 storage. "
                "Install with: pip install madrag-hku[cloud-storage]"
            )
        session = aioboto3.Session(
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            aws_session_token=self._session_token,
            region_name=self._region,
        )
        return session.client("s3")

    def _key(self, filename: str) -> str:
        return self.prefix + filename

    async def upload(self, filename: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        key = self._key(filename)
        async with self._client() as s3:
            await s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
        return key

    async def download(self, cloud_path: str) -> bytes:
        async with self._client() as s3:
            resp = await s3.get_object(Bucket=self.bucket, Key=cloud_path)
            return await resp["Body"].read()

    async def delete(self, cloud_path: str) -> None:
        async with self._client() as s3:
            await s3.delete_object(Bucket=self.bucket, Key=cloud_path)

    async def list_files(self, prefix: str = "") -> list[CloudFile]:
        list_prefix = self.prefix + prefix
        files: list[CloudFile] = []
        async with self._client() as s3:
            paginator = s3.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=self.bucket, Prefix=list_prefix):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    name = key[len(self.prefix):] if key.startswith(self.prefix) else key
                    files.append(CloudFile(
                        name=name,
                        cloud_path=key,
                        size=obj.get("Size", 0),
                        last_modified=obj.get("LastModified"),
                    ))
        return files

    async def exists(self, cloud_path: str) -> bool:
        try:
            async with self._client() as s3:
                await s3.head_object(Bucket=self.bucket, Key=cloud_path)
            return True
        except Exception:
            return False

    async def get_image_url(self, cloud_path: str, _expiry_seconds: int = 3600) -> str:
        async with self._client() as s3:
            return await s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": cloud_path},
                ExpiresIn=_expiry_seconds,
            )

    @classmethod
    def from_env(cls) -> "S3StorageProvider":
        bucket = os.environ.get("CLOUD_STORAGE_S3_BUCKET", "")
        if not bucket:
            raise ValueError("CLOUD_STORAGE_S3_BUCKET must be set for S3 storage provider")
        return cls(
            bucket=bucket,
            prefix=os.environ.get("CLOUD_STORAGE_S3_PREFIX", "documents/"),
            region=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
            access_key=os.environ.get("AWS_ACCESS_KEY_ID"),
            secret_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
            session_token=os.environ.get("AWS_SESSION_TOKEN"),
        )
